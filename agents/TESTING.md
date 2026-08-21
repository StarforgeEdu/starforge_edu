# TESTING.md — the testing playbook

Every lane follows this. DoD item 10 says new code ships with its tests in the same branch — this file defines what "its tests" means. Coverage gates merges (TD-20). The tenant-isolation test (TASKS §26 item 1) is the most load-bearing test in the repo: it exists **before** TD-1 lands, red→green.

---

## 1. Stack & invocation

- **Runner:** `pytest` + `pytest-django` (`pytest.ini` → `DJANGO_SETTINGS_MODULE=config.settings.test`). Test settings already give you: eager Celery (`CELERY_TASK_ALWAYS_EAGER=True`, `CELERY_TASK_EAGER_PROPAGATES=True`, plain `celery.app.task:Task` class), `InMemoryChannelLayer`, locmem cache, `FileSystemStorage`, `ESKIZ_USE_MOCK=True`, MD5 hasher, `ALLOWED_HOSTS=["*"]`.
- **Invocation:** `uv run pytest -q` locally (uses `--reuse-db` from `pytest.ini`; pass `--create-db` after pulling new migrations). CI (`.github/workflows/ci.yml` test job) runs against real Postgres 16 + Redis 7 services and always builds a fresh DB.
- **Coverage (TD-20):** Day 1 Lane A wires `uv run pytest -q --cov=apps --cov=core --cov-fail-under=70` into the CI test job (TASKS §1). Floor schedule: **70 after Day 1 → 80 after Day 3 → 85 after Day 5**. Bumping the number in `ci.yml` is the EOD duty of D1-A, D3-F, D5-B respectively.
- **Markers** — Day 1 Lane E appends to `pytest.ini` (`--strict-markers` is on; unregistered markers fail):

```ini
markers =
    integration: tests that hit the database / external services
    slow: > ~2s (tenant provisioning, PDF generation); excluded from quick runs
    channels: websocket tests (transaction=True, asyncio)
```

  Quick inner loop: `uv run pytest -q -m "not slow and not integration"`.
- **Test layout:** `apps/<app>/tests/` package (`factories.py`, `test_api.py`, `test_services.py`, `test_tasks.py`); cross-cutting suites in a top-level `tests/` (permission matrix, migration guard, webhook attacks, e2e helpers). Day 1 Lane E extends `pytest.ini` `testpaths` to `apps core infrastructure tests` (additive).
- **Parallelization (decision):** add `pytest-xdist` to the dev group Day 1 Lane E (TD-16, justify in WORKLOG). Local use is optional (`-n auto`; each worker gets its own `test_starforge_gwN` DB and pays tenant setup once, amortized by `--reuse-db`). **CI stays serial through Day 2**; Day 3 Lane F flips the CI test step to `-n auto` once the suite exceeds ~3 minutes. `pytest-cov` combines xdist data automatically.

---

## 2. Tenant fixtures — the root `conftest.py` (Day 1 Lane E implements exactly this)

django-tenants schema creation runs the full `TENANT_APPS` migration graph per tenant — seconds each. Pay it **once per session** by hooking `django_db_setup`; everything else is cheap. Function-scoped fixtures self-heal after transactional (`transaction=True`) tests flush the public schema. Create `conftest.py` at the repo root:

```python
"""Root conftest — two-tenant fixture set. Spec: agents/TESTING.md §2."""
import factory.random
import pytest
from django_tenants.utils import schema_context
from rest_framework.test import APIClient

TENANTS = {"tenant_a": "a.localhost", "tenant_b": "b.localhost"}

def _ensure_tenants() -> None:
    from apps.tenancy.models import Center
    from apps.tenancy.services import provision_center
    for slug, host in TENANTS.items():
        if not Center.objects.filter(schema_name=slug).exists():
            # Cheap if the schema already exists (django-tenants skips
            # create_schema via check_if_exists) — only the rows are restored.
            provision_center(name=slug.replace("_", " ").title(), slug=slug, primary_domain=host)

@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Provision tenant_a + tenant_b once per session (the slow part)."""
    with django_db_blocker.unblock():
        _ensure_tenants()

@pytest.fixture(scope="session", autouse=True)
def _deterministic_seeds():
    factory.random.reseed_random("starforge")  # CI == local, always

def _get_tenant(slug):
    from apps.tenancy.models import Center
    _ensure_tenants()  # self-heal after a transaction=True flush
    return Center.objects.get(schema_name=slug)

@pytest.fixture
def tenant_a(db):
    return _get_tenant("tenant_a")

@pytest.fixture
def tenant_b(db):
    return _get_tenant("tenant_b")

@pytest.fixture
def api_client():
    return APIClient()  # host = "testserver" → public schema; tenant views 400

@pytest.fixture
def client_for():
    """client_for(tenant) → APIClient with the tenant's host pre-bound.
    django-tenants resolves schema from the Host header — never call a
    tenant endpoint without it."""
    def _make(tenant):
        host = tenant.domains.get(is_primary=True).domain
        return APIClient(HTTP_HOST=host)
    return _make

@pytest.fixture
def user_in():
    """user_in(tenant, roles=[...]) → User in that tenant's schema with
    RoleMemberships (creates a Branch if none supplied)."""
    def _make(tenant, *, roles=(), branch=None, **kwargs):
        from apps.users.tests.factories import UserFactory
        from apps.users.models import RoleMembership
        from apps.org.tests.factories import BranchFactory
        with schema_context(tenant.schema_name):
            user = UserFactory(**kwargs)
            if roles:
                branch = branch or BranchFactory()
                for role in roles:
                    RoleMembership.objects.create(user=user, branch=branch, role=role)
        return user
    return _make

@pytest.fixture
def as_user(client_for):
    """as_user(tenant, user) → authed APIClient. Mints a REAL token pair via
    apps.auth.services.issue_token_pair inside the tenant schema, so the
    TD-1 `schema`/`tv` claims are exercised for free once Lane C ships them."""
    def _make(tenant, user):
        from apps.auth.services import issue_token_pair
        with schema_context(tenant.schema_name):
            access = issue_token_pair(user)["access"]
        client = client_for(tenant)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client
    return _make

@pytest.fixture
def as_role(tenant_a, user_in, as_user):
    """as_role("teacher") → (client, user) on tenant_a. The matrix workhorse."""
    def _make(role, tenant=None):
        tenant = tenant or tenant_a
        user = user_in(tenant, roles=[role])
        return as_user(tenant, user), user
    return _make

@pytest.fixture
def sms_outbox():
    from infrastructure.sms.eskiz_client import MockEskizClient
    MockEskizClient.outbox.clear()
    return MockEskizClient.outbox
```

Rules:
- **Never** hit a tenant endpoint without the host header. Explicit form: `client.get("/api/v1/students/", HTTP_HOST="a.localhost")`. `TenantSafeModelViewSet` (`core/viewsets.py`) raises `tenant_required` (400) on the public schema — a test that forgets the host fails loudly, by design.
- **Never** create tenant-schema objects outside `schema_context(tenant.schema_name)` — they silently land in `public` and your test lies to you.
- `sms_outbox` requires a one-line change Day 1 Lane E: give `MockEskizClient` a `ClassVar` `outbox: list[dict]` and append `{"phone": phone, "text": text}` in `send()` (`infrastructure/sms/eskiz_client.py`). Lane A edits the same file for TD-17 — Lane E merges last on Day 1, rebase first.

---

## 3. The mandatory per-endpoint matrix (DoD item 10)

For **every** endpoint a lane ships, all five categories — no exceptions, no "covered implicitly":

| # | Category | Requirement |
|---|---|---|
| 1 | Happy path | one test per **allowed** role (from `ROLE_PERMISSION_MATRIX` in `core/permissions.py`) |
| 2 | Denied | ≥ 2 disallowed roles → 403 `forbidden` envelope, **plus** anonymous → 401 |
| 3 | Cross-tenant | tenant_a token + tenant_a object id via tenant_b host → 401/404; **after TD-1 lands: exactly 401 `tenant_mismatch`** |
| 4 | Validation | the 2–3 edges that matter (bad enum, past date, over-cap, duplicate unique) → 400 with `error.fields` |
| 5 | List shape & queries | pagination envelope `{count, next, previous, results}` (`core/pagination.py` DefaultPagination) + query budget that does **not** grow with row count |

Copy-paste template (adapt per app — this is the contract, not a suggestion):

```python
import pytest
from django_tenants.utils import schema_context
from core.permissions import Role

pytestmark = pytest.mark.django_db
URL = "/api/v1/students/"


class TestStudentEndpointMatrix:
    @pytest.mark.parametrize("role", [Role.DIRECTOR, Role.REGISTRAR, Role.HEAD_OF_DEPT])
    def test_list_allowed(self, as_role, role):
        client, _ = as_role(role)
        assert client.get(URL).status_code == 200

    @pytest.mark.parametrize("role", [Role.CASHIER, Role.SECURITY])
    def test_list_denied(self, as_role, role):
        resp = as_role(role)[0].get(URL)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    def test_anonymous_denied(self, tenant_a, client_for):
        assert client_for(tenant_a).get(URL).status_code == 401

    def test_cross_tenant_token_rejected(self, tenant_a, tenant_b, user_in, client_for):
        from apps.auth.services import issue_token_pair
        user = user_in(tenant_a, roles=[Role.DIRECTOR])
        with schema_context(tenant_a.schema_name):
            access = issue_token_pair(user)["access"]
        client_b = client_for(tenant_b)
        client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = client_b.get(URL)
        assert resp.status_code == 401  # post-TD-1, additionally:
        # assert resp.json()["error"]["code"] == "tenant_mismatch"

    def test_create_validation_edges(self, as_role):
        client, _ = as_role(Role.REGISTRAR)
        resp = client.post(URL, {"enrollment_date": "2099-01-01"}, format="json")
        assert resp.status_code == 400

    def test_list_query_budget(self, as_role, tenant_a, django_assert_max_num_queries):
        from apps.students.tests.factories import StudentProfileFactory
        client, _ = as_role(Role.DIRECTOR)
        with schema_context(tenant_a.schema_name):
            StudentProfileFactory.create_batch(50)
        with django_assert_max_num_queries(10):   # fixed budget; MUST NOT scale with rows
            body = client.get(URL).json()
        assert set(body) == {"count", "next", "previous", "results"}
```

Budget guidance: tenant resolution (1) + JWT user (1) + role memberships (1) + count (1) + page (1) + one per `prefetch_related` — declare a number ≤ 10 and pin it. A budget failure is an N+1 bug in `selectors.py`, never a reason to raise the number silently.

The cross-tenant test above, written against `/api/v1/users/me/` **before** TD-1 lands, is TASKS §26 item 1 — Day 1 Lane E writes it red, Day 1 Lane C turns it green. The pure-matrix test (TASKS §3 "Permission test matrix", §26 item 8) lives in `tests/test_permission_matrix.py`: parameterize over `(role, "resource:verb") → has_permission_code(...)` for every entry in `ROLE_PERMISSION_MATRIX`, and keep it updated as lanes add real entries (TD-5).

---

## 4. Factories

`factory-boy` is the standard (TD-16; Day 1 Lane E adds it and **removes `model-bakery`** from dev deps — one tool, not two).

- One `factories.py` per app under `apps/<app>/tests/`. Name `<Model>Factory`, `Meta.model` the real model, `django_get_or_create` on natural keys.
- **SubFactory chains follow the domain graph:** `UserFactory` → `StudentProfileFactory(user=SubFactory)` → `CohortMembershipFactory(student=…, cohort=…)`. A factory never reaches into another **role app** directly (no `StudentProfileFactory` field pointing at teachers/parents) — relationships route through link models exactly like the code does (`Guardian` in apps/parents, `CohortMembership` in apps/cohorts, attendance/academics rows): mirror `docs/adding-an-app.md` layering.
- **Phones are never Faker:** `phone = factory.Sequence(lambda n: f"+99890{n:07d}")` — must pass `core.validators.normalize_phone` (E.164). Faker ships no `uz_UZ` locale; use `factory.Faker("name", locale="ru_RU")` for person/address strings (Cyrillic is realistic for UZ). If Latin-script Uzbek names matter for a test, hardcode a small list in the factory — don't invent a provider dependency.
- **Determinism:** the autouse `_deterministic_seeds` fixture (conftest §2) calls `factory.random.reseed_random("starforge")` — CI and local produce identical data. Never call `random` directly in a factory; use `factory.Faker`/`factory.Sequence`.
- Factories **may** call services where the service is the only valid constructor (e.g. enrollment state machine): wrap with `@factory.django.mute_signals` only when a test explicitly isolates from notifications, and say why in a comment.

---

## 5. Time

**Decision: `time-machine`** (C-speed, Python 3.13-ready, patches everything including `time.time` in C extensions). Day 1 Lane E adds it to the dev group — record as a TD-16 addition in WORKLOG. Do not use `freezegun` or hand-mock `timezone.now`.

| Scenario | Pattern |
|---|---|
| OTP expiry (TASKS §3) | issue OTP, `time_machine.travel(now + timedelta(seconds=settings.OTP_TTL_SECONDS + 1))`, verify → 400 |
| Attendance correction window (TASKS §10) | mark record, travel +25h (window from `CenterSettings`, TD-13), amend → 403 without director approval |
| Trial expiry / paywall (TD-8) | set `Center.trial_ends_at` in the past, run billing beat task, tenant API → 402 `subscription_required` |
| Auto-absent (TASKS §10) | lesson at T, travel T+31min, run `mark_absent_after_lesson`, assert absent records exist; travel T+10min first and assert they don't |
| Quiet hours (TASKS §17) | `time_machine.travel("2026-06-10 23:30 +05:00")` (Asia/Tashkent), `dispatch()` → SMS deferred, in-app still delivered |

Always travel with explicit tz offsets — the platform is `Asia/Tashkent`; naive datetimes in tests are a bug. Use the decorator form `@time_machine.travel(..., tick=False)` unless the test needs elapsing time.

---

## 6. Celery

Eager mode is already on in `config/settings/test.py`, with the **plain** `celery.app.task:Task` class — tenant-schemas-celery's schema activation is bypassed in tests. Consequences and prescriptions:

- **Test task bodies as functions** inside `schema_context(tenant.schema_name)`. The §26 "Celery task isolation" item is satisfied semantically: run the task body under `schema_context("tenant_a")`, assert effects exist in tenant_a and **assert the same query in tenant_b's schema_context returns nothing**. (True broker-level `_schema_name` routing is exercised only by the Day 5 E2E demo against the compose worker — document this limitation, don't fake it.)
- **Idempotency (TASKS §22):** every external-touching task stores an idempotency key on the source row. Test = call the task **twice** with identical args, assert exactly one side effect (one entry in `sms_outbox`, one `FiscalReceipt`, one allocation). This test is mandatory for every task added.
- **Retries:** monkeypatch the transport to raise (`requests.ConnectionError`), call `task.apply()`; with `CELERY_TASK_EAGER_PROPAGATES=True` a `self.retry()` surfaces as `celery.exceptions.Retry` — assert it, and assert the source row is unchanged (resumable). For `autoretry_for` tasks assert `task.max_retries == 3` and backoff settings directly.
- **Beat registry (Day 4 Lane F consolidation, TASKS §22):** one test in `tests/test_beat_schedule.py`:

```python
def test_every_beat_entry_resolves():
    from config.celery import app
    for name, entry in app.conf.beat_schedule.items():
        assert entry["task"] in app.tasks, f"beat entry {name} points at unregistered task"
```

---

## 7. Channels

`InMemoryChannelLayer` is configured; use `channels.testing.WebsocketCommunicator` against the real ASGI app so `TenantAwareJWTAuthMiddleware` (`infrastructure/websocket/middleware.py`) is in the loop. Channels tests: `@pytest.mark.channels @pytest.mark.asyncio @pytest.mark.django_db(transaction=True)`. `transaction=True` flushes the DB at teardown — the §2 fixtures self-heal, but keep these tests few and focused.

```python
import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from config.asgi import application

HOST_HEADERS = [(b"host", b"a.localhost")]
pytestmark = [pytest.mark.channels, pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def test_anonymous_rejected(tenant_a):
    comm = WebsocketCommunicator(application, "/ws/ping/", headers=HOST_HEADERS)
    connected, close_code = await comm.connect()
    assert not connected and close_code == 4401   # PingConsumer close code

async def test_authenticated_hello(tenant_a, user_in, access_token_for):
    token = access_token_for(tenant_a, user_in(tenant_a))   # sync_to_async-wrapped helper
    comm = WebsocketCommunicator(application, f"/ws/ping/?token={token}", headers=HOST_HEADERS)
    connected, _ = await comm.connect()
    assert connected
    assert (await comm.receive_json_from())["type"] == "hello"
    await comm.disconnect()

async def test_group_fanout(tenant_a, connected_notification_comm):
    await get_channel_layer().group_send("user.1", {"type": "notify", "payload": {"id": 1}})
    assert (await connected_notification_comm.receive_json_from())["payload"]["id"] == 1
```

Required coverage when Day 4 Lane C ships real consumers (TD-15, TASKS §21): anonymous → 4401; authenticated hello; **tenant resolution** — connect with `b.localhost` host + tenant_a token → rejected; group fan-out — `apps.notifications.services.dispatch()` → consumer receives over `user.{id}` / `cohort.{id}`; disconnect leaves groups (second `group_send` not received).

---

## 8. External services (TD-2: mock-first)

The boundary rule: **unit tests never open a network connection.** One marked-`integration` test per flow runs against compose services; everything else hits mocks/local backends.

| Service | Unit tests | Integration test |
|---|---|---|
| SMS (Eskiz) [OWNER:O-1] | assert against `sms_outbox` (MockEskizClient outbox, §2) | none needed — mock is the contract until O-1 |
| Click / Payme / Uzum [OWNER:O-3/O-4/O-6] | webhook payload **builders** in `apps/payments/tests/builders.py`: `make_payme_rpc(method, *, tampered_signature=False, replay_nonce=None)`, same for Click/Uzum. Valid, tampered, replayed variants are helpers, not copy-pasted dicts | mock end-to-end via the real public webhook URL (TD-6) |
| Soliq fiscalization [OWNER:O-5] | `MockSoliqClient` (TD-7) returns deterministic fiscal sign + QR URL | none until O-5 |
| S3 / MinIO | `FileSystemStorage` is active; **stub** the presign helpers (`infrastructure/storage/s3_client.py`) with monkeypatch returning canned URLs | one `@pytest.mark.integration` test per flow (upload-url → PUT → confirm) against MinIO; `pytest.mark.skipif(not os.environ.get("MINIO_URL"))`. Day 2 Lane E adds a minio service block to the CI test job (additive) and sets `MINIO_URL` |
| Anthropic [OWNER:O-2] | monkeypatch `infrastructure.ai.anthropic_client.complete` to return the real shape: `{"text": ..., "usage": {"input_tokens": n, "output_tokens": n, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, "stop_reason": "end_turn", "raw_id": "msg_test"}`. Budget tests (TASKS §18) drive `TenantAIBudget` arithmetic off these canned token counts | never — real API calls in CI are forbidden |
| FCM/APNs [OWNER:O-7] | mock push adapter with an outbox, same pattern as SMS | none until O-7 |

Mock clients are settings-switched (`*_USE_MOCK`, default True outside production). A test that flips a mock flag to False without patching the transport is a bug.

---

## 9. Webhook & attack tests — Day 3 Lane F catalog

Required test names (in `tests/test_webhook_attacks.py` + `apps/payments/tests/`); all assert the TD-18 envelope on errors:

- `test_payme_webhook_invalid_signature_rejected`
- `test_click_webhook_invalid_signature_rejected`
- `test_uzum_webhook_invalid_signature_rejected`
- `test_webhook_replayed_nonce_rejected` (nonce stored on `WebhookEvent`, duplicate → rejected, no double-processing)
- `test_webhook_tampered_amount_rejected` (signature valid for different amount)
- `test_webhook_unknown_center_slug_404` (TD-6 URL: `/api/v1/webhooks/<provider>/<center_slug>/`)
- `test_webhook_resolves_correct_tenant_schema` (payment row lands in the right schema, not public, not the other tenant)
- `test_payme_create_transaction_idempotent` (same provider txn id twice → one `Payment`)
- `test_payme_amount_mismatch_returns_jsonrpc_error` (Payme speaks JSON-RPC error codes, not the DRF envelope — the one allowed exception, per Payme protocol)
- `test_webhook_uses_per_tenant_credentials` (tenant_b's `ProviderConfig` secret does not validate a tenant_a webhook — TD-6)
- Auth attacks (Day 1 Lane C/E, TASKS §26): `test_otp_throttle_429_on_4th_request`, `test_otp_wrong_code_5x_invalidates`, `test_refresh_rotation_blacklists_old`, `test_refresh_reuse_revokes_all`, `test_token_version_bump_invalidates_live_access` (TD-1 `tv` claim), `test_cross_tenant_token_rejected` (§3 above).

---

## 10. Migration & schema tests

In `tests/test_migrations.py`:

- **Fresh shared migrate (TASKS §26):** CI builds the test DB from zero on every run — `pytest --create-db` in CI *is* the `migrate_schemas --shared` fresh-DB test (django-tenants overrides `migrate` to run shared migrations). Keep CI on fresh DBs; never add `--reuse-db` to `ci.yml`.
- **Tenant auto-migration (TASKS §26):** provisioning a throwaway Center must yield a fully-migrated schema:

```python
@pytest.mark.slow
@pytest.mark.django_db
def test_provision_center_runs_tenant_migrations():
    from django_tenants.utils import schema_context
    from apps.tenancy.services import provision_center
    center = provision_center(name="Fresh", slug="fresh_check", primary_domain="fresh.localhost")
    with schema_context(center.schema_name):
        from apps.users.models import User
        assert User.objects.count() == 0   # table exists and is queryable
```

- **No pending migrations** — fails the build if anyone changed models without `makemigrations`:

```python
@pytest.mark.django_db
def test_no_pending_migrations():
    from django.core.management import call_command
    try:
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)
    except SystemExit:
        pytest.fail("Model changes without a migration. Run `uv run python manage.py makemigrations`.")
```

---

## 11. Coverage discipline

Day 1 Lane E adds to `pyproject.toml`:

```toml
[tool.coverage.run]
omit = ["*/migrations/*", "*/tests/*", "*/admin.py", "*/apps.py", "conftest.py",
        "manage.py", "scripts/*", "config/asgi.py", "config/wsgi.py", "docker/*"]

[tool.coverage.report]
exclude_also = ["if TYPE_CHECKING:", "raise NotImplementedError", "def __str__", "@abstractmethod"]
```

- Read the report with `uv run pytest --cov=apps --cov=core --cov-report=term-missing` — chase **red lines in `services.py`, `selectors.py`, permission branches, task bodies, webhook handlers**. That's where bugs live.
- Do **not** chase: admin registrations, `apps.py`, migrations, `__str__`, settings, type-checking blocks — they're omitted above; padding coverage with them is cheating the gate.
- Floors (TD-20): 70 (Day 1) → 80 (Day 3) → 85 (Day 5). The floor is `--cov-fail-under` in `ci.yml` — raising it is a one-line PR by the lane named in §1. If your branch drops coverage below the current floor, CI blocks your merge: write the missing tests, don't lower the bar.

---

## 12. E2E — `scripts/e2e_demo.py` (Day 5 Lane F)

The release gate. Runs against the **compose stack** (real Postgres/Redis/MinIO, real worker + beat, Daphne) with development settings and all mocks on — not pytest, plain Python + `requests` + `websockets`. Contract:

- Each ROADMAP §7 acceptance item is one numbered step, executed in order 1→12; print `STEP n PASS/FAIL <detail>`; **exit non-zero on first failure**.
- Steps (= ROADMAP §7 verbatim): 1 two Centers + cross-tenant 401 `tenant_mismatch` · 2 full OTP flow incl. `GET /api/v1/platform/resolve/?slug=` (TD-19), refresh, logout-everywhere · 3 enroll → guardian link → cohort → recurring schedule + rejected conflict · 4 attendance absent → mock SMS + WS notification received live · 5 exam → grades → transcript PDF via signed URL · 6 assignment S3 upload → submission → AI feedback task within budget · 7 invoice → mock Payme webhook → allocation + fiscal receipt → reconciliation matches · 8 trial expiry → 402 → reactivate via control center · 9 print job queued → agent token claim → done · 10 scheduled report in S3 + mock email link · 11 `pytest --cov` ≥ 85, ruff, mypy, OpenAPI validate, TS+Dart clients generate · 12 list-endpoint latency < 150 ms on seeded data, zero N+1.
- Idempotent: re-runnable on the same stack (use timestamped slugs for the two Centers).
- Tag `v1.0.0` only when this script exits 0 — that is the definition of done for the whole operation.

---

## Quick reference — what every lane runs before pushing

```
uv run ruff check . && uv run ruff format --check .
uv run mypy apps core infrastructure config
uv run pytest -q --cov=apps --cov=core --cov-fail-under=<current floor>
```

Red master? Fixing it is your first task (ROADMAP §2.2). Tests are not a lane — they are every lane.
