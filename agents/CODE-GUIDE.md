# CODE-GUIDE — How to Write Code in This Repo

> Read `ROADMAP.md` first (operating model, DoD, TD-1..TD-20). This file is second. Every pattern here is extracted from the best existing code in the repo — your code must be indistinguishable from it. When this guide and ROADMAP disagree, ROADMAP wins; report the conflict in `agents/WORKLOG.md`.

---

## 1. Architecture in 60 seconds

Schema-per-tenant via django-tenants. One `Center` row = one Postgres schema. Hostname picks the schema; everything downstream is automatically isolated.

```
request: https://demo.starforge.uz/api/v1/attendance/records/
   │
   ▼
TenantMainMiddleware (FIRST in MIDDLEWARE, config/settings/base.py)
   │  Domain row "demo.starforge.uz" → Center(schema_name="demo")
   │  → connection.schema_name = "demo"; search_path set
   ▼
URLConf: config/urls.py (tenant)  |  config/urls_public.py (apex/public)
   ▼
TenantSafeModelViewSet (core/viewsets.py)
   │  initial() raises TenantContextMissing if schema is public  ← the guard
   ▼
RolePermission + ObjectScopedPermission (core/permissions.py)
   │  ROLE_PERMISSION_MATRIX["teacher"] ⊇ {"attendance:*"} ?  fail-closed (TD-4)
   ▼
Serializer (validate input shape)  →  service (writes) / selector (reads)
   ▼
Model → Postgres (tenant schema)   …   signal → apps/notifications → Celery
   ▼
Response in TD-18 envelope; errors via core/exceptions.drf_exception_handler
```

Key files: `core/viewsets.py`, `core/permissions.py`, `core/exceptions.py`, `core/pagination.py`, `config/settings/base.py` (SHARED_APPS/TENANT_APPS split), `config/urls.py` + `config/urls_public.py`, `docs/architecture.md`.

---

## 2. The layering law

Every app follows `docs/adding-an-app.md`: **models → services.py (writes) → selectors.py (reads) → serializers → views → urls**.

| Layer | Rules |
|---|---|
| `models.py` | Schema + constraints only. No business logic beyond trivial properties. |
| `services.py` | ALL writes. Keyword-only typed signatures, `@transaction.atomic`, validate inputs, raise `StarforgeError` subclasses (`core/exceptions.py`), emit signals via `transaction.on_commit`. |
| `selectors.py` | ALL non-trivial reads. Always `select_related`/`prefetch_related`. Return querysets, not lists. Role-based scoping lives here (§4). |
| `serializers.py` | Read vs write split when shapes differ. Never `fields = "__all__"` on models with sensitive fields. |
| `views.py` | Thin. Wire `required_perms` + serializer + service/selector. Zero business logic. |

The canonical service in the repo is `apps/auth/services.py` — study `send_otp`/`verify_otp`: keyword-only args, `@transaction.atomic`, `select_for_update`, settings-driven knobs, `StarforgeError` subclasses. Copy that shape.

### The worked example: attendance marking (TASKS §10, D2-B)

This is the template everyone copies. Five files, every rule applied.

**`apps/attendance/models.py`**
```python
from django.db import models
from django.utils.translation import gettext_lazy as _


class AttendanceRecord(models.Model):
    class Status(models.TextChoices):
        PRESENT = "present", _("Present")
        ABSENT = "absent", _("Absent")
        LATE = "late", _("Late")
        EXCUSED = "excused", _("Excused")

    lesson = models.ForeignKey("schedule.Lesson", on_delete=models.CASCADE,
                               related_name="attendance_records")
    student = models.ForeignKey("students.StudentProfile", on_delete=models.CASCADE,
                                related_name="attendance_records")
    status = models.CharField(max_length=8, choices=Status.choices)
    note = models.CharField(max_length=512, blank=True)
    marked_by = models.ForeignKey("users.User", on_delete=models.SET_NULL,
                                  null=True, related_name="+")
    marked_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-marked_at",)
        constraints = [
            models.UniqueConstraint(fields=("lesson", "student"),
                                    name="attendance_one_record_per_lesson_student"),
        ]
        indexes = [models.Index(fields=("student", "status"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.student_id}@{self.lesson_id}:{self.status}"
```

**`apps/attendance/signals.py`**
```python
import django.dispatch

# kwargs: record_id, status, schema_name
attendance_marked = django.dispatch.Signal()
```

**`apps/attendance/services.py`**
```python
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.attendance.models import AttendanceRecord
from apps.attendance.signals import attendance_marked
from apps.org.selectors import get_center_settings  # TD-13 accessor (§10)
from apps.schedule.models import Lesson
from core.exceptions import ValidationException


@transaction.atomic
def mark_attendance(*, lesson_id: int, student_id: int, status: str,
                    marked_by, note: str = "") -> AttendanceRecord:
    """Create or amend one record. Idempotent per (lesson, student)."""

    lesson = Lesson.objects.select_related("cohort").get(pk=lesson_id)
    if not lesson.cohort.memberships.filter(student_id=student_id).exists():
        raise ValidationException(_("Student is not in this lesson's cohort."))

    window = get_center_settings().attendance_correction_window_hours  # no magic numbers
    if timezone.now() > lesson.starts_at + timedelta(hours=window):
        raise ValidationException(_("Correction window closed; needs director approval."))

    record, _created = AttendanceRecord.objects.update_or_create(
        lesson_id=lesson_id, student_id=student_id,
        defaults={"status": status, "note": note, "marked_by": marked_by},
    )
    transaction.on_commit(lambda: attendance_marked.send(
        sender=AttendanceRecord, record_id=record.pk,
        status=status, schema_name=connection.schema_name,
    ))
    return record
```

**`apps/attendance/selectors.py`**
```python
from django.db.models import QuerySet

from apps.attendance.models import AttendanceRecord


def attendance_for_lesson(*, lesson_id: int) -> QuerySet[AttendanceRecord]:
    return (
        AttendanceRecord.objects.filter(lesson_id=lesson_id)
        .select_related("student__user", "marked_by")
        .order_by("student__user__last_name")
    )
```

**`apps/attendance/serializers.py`** — read/write split:
```python
from rest_framework import serializers

from apps.attendance.models import AttendanceRecord


class AttendanceRecordReadSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.user.get_full_name", read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = ("id", "lesson", "student", "student_name", "status",
                  "note", "marked_by", "marked_at")


class AttendanceMarkSerializer(serializers.Serializer):
    lesson = serializers.IntegerField()
    student = serializers.IntegerField()
    status = serializers.ChoiceField(choices=AttendanceRecord.Status.choices)
    note = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")
```

**`apps/attendance/views.py`**
```python
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.attendance import selectors, services
from apps.attendance.serializers import AttendanceMarkSerializer, AttendanceRecordReadSerializer
from core.viewsets import TenantSafeModelViewSet


class AttendanceRecordViewSet(TenantSafeModelViewSet):
    serializer_class = AttendanceRecordReadSerializer
    resource = "attendance"                      # TD-5: derives list/retrieve → attendance:read
    required_perms = {"mark": "attendance:write"}  # custom actions declared explicitly
    object_scope = "branch"
    filterset_fields = ("lesson", "student", "status")
    ordering_fields = ("marked_at",)
    http_method_names = ["get", "post", "head", "options"]  # no raw PUT/DELETE on records

    def get_queryset(self):
        return selectors.scoped_attendance_records(user=self.request.user)  # §4 scoping

    @extend_schema(summary="Mark attendance for one student in one lesson",
                   request=AttendanceMarkSerializer,
                   responses={201: AttendanceRecordReadSerializer}, tags=["attendance"])
    @action(detail=False, methods=["post"])
    def mark(self, request):
        ser = AttendanceMarkSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        record = services.mark_attendance(marked_by=request.user, **{
            "lesson_id": ser.validated_data["lesson"],
            "student_id": ser.validated_data["student"],
            "status": ser.validated_data["status"],
            "note": ser.validated_data["note"],
        })
        return Response(AttendanceRecordReadSerializer(record).data,
                        status=status.HTTP_201_CREATED)
```

**`apps/attendance/urls.py`**
```python
from rest_framework.routers import DefaultRouter

from apps.attendance.views import AttendanceRecordViewSet

router = DefaultRouter()
router.register("records", AttendanceRecordViewSet, basename="attendance-records")
urlpatterns = router.urls
```

The consumer of the signal lives in **`apps/notifications/receivers.py`** (§7) — the attendance app never touches SMS/push itself.

---

## 3. Tenancy rules

1. **Never query tenant models from public-schema code paths.** `TenantSafeModelViewSet.initial()` (`core/viewsets.py`) raises `TenantContextMissing` when `connection.schema_name` is public — every tenant CRUD view must inherit it. Non-CRUD tenant APIViews must perform the same check or subclass a guard mixin.
2. **Celery.** `CELERY_TASK_CLS = "tenant_schemas_celery.task:TenantTask"` (`config/settings/base.py`) auto-propagates the current schema when you `.delay()` from a request. When dispatching from a context that knows the tenant by name (beat jobs, webhook handlers, signals carrying `schema_name`), pass it explicitly: `my_task.delay(obj_id, _schema_name=schema_name)`. Beat jobs that must run for every tenant iterate `Center.objects.filter(is_active=True)` and enter `schema_context(center.schema_name)` per tenant.
3. **Scripts / management commands** wrap tenant work in `from django_tenants.utils import schema_context` — see `scripts/seed_dev.py` for the pattern, or use `tenant_command`.
4. **SHARED_APPS vs TENANT_APPS decision rule** (`config/settings/base.py`): platform-level data the apex needs (tenancy `Center`/`Domain`, billing TD-8, and per TD-3 `apps.users` + `apps.auth` + `rest_framework_simplejwt.token_blacklist` for platform staff) goes in `SHARED_APPS`; everything a school owns goes in `TENANT_APPS` only. Apps in both get a table in public AND every tenant schema — that is deliberate for users (TD-3), accidental and forbidden for anything else.
5. **Webhook exception (TD-6)** — the one sanctioned public→tenant hop:

```python
# routed in config/urls_public.py: api/v1/webhooks/<provider>/<center_slug>/
from django_tenants.utils import schema_context
from apps.tenancy.models import Center

def handle(request, provider: str, center_slug: str):
    center = Center.objects.filter(slug=center_slug, is_active=True).first()
    if center is None:
        return error_response("not_found", status=404)        # TD-18 envelope, always
    with schema_context(center.schema_name):
        config = ProviderConfig.objects.get(provider=provider)  # credentials: TD-11 encrypted
        verify_signature(request, config)                       # BEFORE touching any row
        process_webhook.delay(event_id, _schema_name=center.schema_name)
```

6. **The load-bearing invariant**: a JWT minted in tenant A must 401 (`tenant_mismatch`) in tenant B (TD-1). The isolation test (TASKS §26 item 1) is written before TD-1 lands. Never weaken it.

---

## 4. Permissions (TD-4, TD-5)

Day 1 Lane C upgrades `core/permissions.py`. Target code (this is what lands — write all new viewsets against it):

```python
DEFAULT_VERB_FOR_ACTION = {
    "list": "read", "retrieve": "read",
    "create": "write", "update": "write", "partial_update": "write", "destroy": "write",
}

class RolePermission(BasePermission):
    """TD-5 per-action; TD-4 fail-closed: no declaration => deny."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        action = getattr(view, "action", None) or request.method.lower()
        required = getattr(view, "required_perms", {}).get(action)
        if required is None:
            resource = getattr(view, "resource", None)
            verb = DEFAULT_VERB_FOR_ACTION.get(action)
            if resource is None or verb is None:
                return False                      # TD-4: deny, do NOT fall through
            required = f"{resource}:{verb}"
        return has_permission_code(_user_roles(user), required)
```

Rules:
- Declare `resource = "<name>"` on every viewset; add `required_perms = {"<custom_action>": "<resource>:<verb>"}` for every `@action`. The legacy flat `required_perm` string (still on all scaffold viewsets, e.g. `apps/attendance/views.py`) is **removed** as each app is rebuilt — never write new code with it.
- The current code's `if required is None: return True` is the TD-4 bug. It dies Day 1. Never reintroduce permissive fallbacks.
- Set `object_scope = "branch" | "department"` on any viewset whose objects carry `branch_id`/`department_id` — `ObjectScopedPermission` (`core/permissions.py`) checks `RoleMembership` rows.
- **`read_self` / `read_own_children` verbs are enforced by queryset scoping in selectors**, not by the permission class:

```python
# apps/attendance/selectors.py
from core.permissions import Role

STAFF_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT, Role.TEACHER, Role.REGISTRAR, Role.IT}

def scoped_attendance_records(*, user) -> QuerySet[AttendanceRecord]:
    qs = AttendanceRecord.objects.select_related("student__user", "lesson", "marked_by")
    roles = {m.role for m in user.role_memberships.all()}
    if roles & STAFF_ROLES or user.is_superuser:
        return qs
    if Role.PARENT in roles:   # read_own_children
        return qs.filter(student__guardian_links__parent__user=user)
    if Role.STUDENT in roles:  # read_self
        return qs.filter(student__user=user)
    return qs.none()           # fail closed here too
```

- When you ship a feature, replace the coarse stub entries in `ROLE_PERMISSION_MATRIX` (`core/permissions.py`) with real per-feature codes. The matrix file is shared — **additive edits only**, never reorder others' entries (ROADMAP §2.1).
- Every permission change needs a row in the parameterized permission-matrix test (TASKS §3, §26; `agents/TESTING.md`).

---

## 5. Models

Follow `apps/users/models.py` — it is the house style (constraints, choices, indexes, docstrings).

- **Constraints**: `UniqueConstraint` (named, e.g. `user_phone_or_email_required` style) for invariants; `CheckConstraint` for data rules. DB enforces what serializers merely suggest.
- **Indexes**: every FK gets one for free; add `models.Index` for every field combination your selectors filter on (`OTP.Meta.indexes` → `("identifier", "consumed_at")` is the model citizen). An unindexed filter is a DoD violation (DoD #12).
- **Choices**: `models.TextChoices` for all new enums (the older `CHANNEL_CHOICES` tuple style in `users/models.py` predates this rule — don't copy it).
- **Encrypted fields (TD-11)**: `core/fields.py` `EncryptedTextField`/`EncryptedCharField` (lands D1) for `national_id`, `medical_notes`, provider credentials, Soliq tokens. Key from `settings.FIELD_ENCRYPTION_KEY` `[OWNER:O-11]` — dev key generated locally, never committed.
- **Timestamps**: `created_at = DateTimeField(auto_now_add=True)`, `updated_at = DateTimeField(auto_now=True)` on every domain model.
- **Soft delete** only where the spec prescribes it (Branch archive TASKS §2, cohort archive §8, user deactivation §3): `is_active` flag or `archived_at` timestamp + selector-level filtering. Do not invent a global soft-delete framework.
- `__str__` and `Meta.ordering` on every model (DoD #1).

**Migration hygiene** (zero migrations exist until D1-A generates them — after that):
- One logical change per migration; never edit a migration that has been merged to `master`; never edit another lane's migration.
- Number conflict (two lanes both created `000X_`): the later merger runs `python manage.py makemigrations --merge` or renumbers their own (ROADMAP §2.3).
- After any model change: `makemigrations <app>`, inspect the file, then verify `migrate_schemas` runs on a fresh DB (tests cover this, TASKS §26).

---

## 6. External services

Pattern: **ABC + real client + mock + settings factory**, exactly as `infrastructure/sms/eskiz_client.py`:

```python
class SMSClient(ABC):
    @abstractmethod
    def send(self, *, phone: str, text: str) -> dict[str, Any]: ...

class MockEskizClient(SMSClient): ...   # deterministic, logs, returns {"mock": True}
class EskizClient(SMSClient): ...       # real HTTP, timeouts on every request

def get_sms_client() -> SMSClient:
    if settings.ESKIZ_USE_MOCK:         # *_USE_MOCK defaults True outside production
        return MockEskizClient()
    return EskizClient(base_url=settings.ESKIZ_API_URL, ...)
```

Apply this shape to Click/Payme/Uzum (`infrastructure/payments/` currently raise `NotImplementedError`), Soliq (TD-7, `infrastructure/fiscal/soliq_client.py`), FCM. Owner credential gates: Eskiz `[OWNER:O-1]`, Anthropic `[OWNER:O-2]`, Click `[OWNER:O-3]`, Payme `[OWNER:O-4]`, Soliq `[OWNER:O-5]`, Uzum `[OWNER:O-6]`, FCM/APNs `[OWNER:O-7]`. Per **TD-2 nothing blocks on these** — build and test against the mock, flip the env flag when `agents/OWNER-ACTIONS.md` delivers.

**Celery-only, with retry + idempotency** (DoD #9, TASKS §22). No sync external HTTP in any request handler — ever. Template:

```python
# celery_tasks/fiscal_tasks.py
from config.celery import app
from core.utils import stable_hash

@app.task(bind=True, max_retries=3, autoretry_for=(requests.RequestException,),
          retry_backoff=True, retry_backoff_max=600, retry_jitter=True)
def submit_fiscal_receipt(self, payment_id: int) -> None:
    payment = Payment.objects.get(pk=payment_id)
    if payment.fiscal_submitted_at:                       # idempotent re-run
        return
    key = stable_hash(f"fiscal:{connection.schema_name}:{payment.pk}")  # core/utils.py
    result = get_fiscal_client().submit(payment=payment, idempotency_key=key)
    payment.fiscal_sign = result["sign"]
    payment.fiscal_submitted_at = timezone.now()
    payment.save(update_fields=["fiscal_sign", "fiscal_submitted_at"])
```

Store the idempotency key / completion marker **on the source row** so a retried task is a no-op (TASKS §22). The Anthropic wrapper (`infrastructure/ai/anthropic_client.py`) already layers Redis response caching over Anthropic prompt caching — call it only from `apps/ai` Celery tasks after a `TenantAIBudget` pre-flight check (TASKS §18).

**Domain apps never import adapters.** `apps/attendance` importing `get_sms_client` is a review-blocking violation. Emit a signal; `apps/notifications` routes (§7). The only legitimate adapter callers: `apps/auth` (OTP, grandfathered), `apps/notifications`, `apps/payments`/`finance` tasks for their providers, `apps/ai` tasks, storage/report tasks.

---

## 7. Signals & events

- **Definition**: `apps/<app>/signals.py` — module-level `django.dispatch.Signal()` instances, past-tense names (`attendance_marked`, `payment_completed`, `grade_published`).
- **Emission**: from services only, inside `transaction.on_commit(...)` so listeners never see uncommitted data. Payload kwargs are flat primitives: `*_id` ints, status strings, and **always `schema_name=connection.schema_name`** so receivers can dispatch Celery across contexts.
- **Receivers**: `apps/<app>/receivers.py`, connected in `apps.py`:

```python
# apps/notifications/apps.py
class NotificationsConfig(AppConfig):
    name = "apps.notifications"
    def ready(self) -> None:
        from . import receivers  # noqa: F401

# apps/notifications/receivers.py
@receiver(attendance_marked, dispatch_uid="notifications.attendance_marked")
def on_attendance_marked(sender, *, record_id, status, schema_name, **kwargs):
    if status == AttendanceRecord.Status.ABSENT:
        dispatch_absence_notification.delay(record_id, _schema_name=schema_name)
```

- **What must emit** (DoD #8): every user-facing happening (enrolled, absent, graded, invoice issued, payment received, assignment due) emits a signal consumed by `apps/notifications`; `apps/notifications/services.dispatch()` is the single fan-out for SMS/email/push/in-app/WebSocket (TD-15).
- **Audit (TD-9)**: sensitive models (User, RoleMembership, Invoice, Payment, Grade, ExamResult, ProviderConfig, Subscription) get `post_save`/`post_delete` receivers in `apps/audit/receivers.py`; non-model events (login, OTP, impersonation, exports) call the `audit_log()` helper. You do not write audit code in your domain app — you make sure your model is on the audit list and your service calls `audit_log()` for non-model events.
- Always pass `dispatch_uid` to `@receiver` — double registration sends double SMS.

---

## 8. API surface

- **Envelope (TD-18, converged in FI-1)**: everything under `/api/v1/` answers in ONE flat error shape `{"success": false, "code", "message", "errors"?}` — byte-identical from `core.responses.error()` (layered views), `core/exceptions.drf_exception_handler` (the DRF `reports` app), and Django's own `handler404/400/403/500` + the middleware 401/402/403/503. (Deliberate exceptions: `/healthz/*` keeps its ops-probe shape; payment webhooks return provider-exact bodies.) Raise `StarforgeError` subclasses (`ValidationException`, `PermissionException`, `NotFoundException`, `ThrottledException`, `TenantContextMissing`); never hand-build error dicts in views.
- **`@extend_schema` on every endpoint** (DoD #7). Minimum bar:

```python
@extend_schema(
    summary="Mark attendance for one student in one lesson",
    description="Teacher-only. Idempotent per (lesson, student). Window per CenterSettings.",
    request=AttendanceMarkSerializer,
    responses={201: AttendanceRecordReadSerializer,
               400: OpenApiResponse(description="validation_error envelope"),
               403: OpenApiResponse(description="forbidden envelope")},
    tags=["attendance"],
    examples=[OpenApiExample("Mark absent", value={"lesson": 12, "student": 7, "status": "absent"})],
)
```

  The generated schema IS the frontend contract (`agents/API-CONTRACT.md`); a CI job validates it.
- **Filtering/search/ordering** on every list endpoint (DoD #5): `DjangoFilterBackend` is already the default (`config/settings/base.py` `DEFAULT_FILTER_BACKENDS`); declare `filterset_fields` (or a `FilterSet` for ranges), `search_fields`, `ordering_fields`. Every filter field must be indexed (§5).
- **Pagination** (`core/pagination.py`): `DefaultPagination` (page-number, 25/page, max 200) is the project default. Use `TimelinePagination` (cursor on `-created_at`) for append-only feeds: audit log, notification feed, payment history. Never an unpaginated list.
- **i18n**: every user-facing string in serializers/services/models wrapped in `gettext_lazy` (see `core/validators.py`); SMS/email/notification templates need uz/ru/en variants (DoD #11).
- **Idempotency-Key header**: client-initiated mutations that reach an external provider (payment creation, AI generation requests) accept an `Idempotency-Key` header; the service persists it on the row and returns the existing row on replay (TASKS §16).

---

## 9. Performance

- **N+1 is a bug, not a style issue** (DoD #3). Every selector eager-loads. Every list endpoint ships a query-count test (`django_assert_num_queries` / `assertNumQueries` — matrix in `agents/TESTING.md`).
- **Index checklist** before commit: every FK ✓ (automatic), every `filterset_fields` entry ✓, every selector `.filter()` field ✓, composite indexes for hot pairs ✓.
- **Caching**: Redis via `django.core.cache` (configured in `config/settings/base.py`); use `infrastructure/cache/redis_client.get_redis()` only for primitives django-cache lacks (INCR+TTL, pub/sub). May cache: CenterSettings (§10), permission-role lookups per request, AI responses (already done in `infrastructure/ai/anthropic_client.py`), report aggregates. May NOT cache: anything permission-scoped per user without the user in the key, anything tenant-scoped without `connection.schema_name` in the key. Invalidate in a `post_save` receiver, not "eventually".
- **No unbounded `.all()`** — list endpoints paginate; tasks iterate with `.iterator()` over large sets; bulk writes use `bulk_create`/`bulk_update`.
- Target: every list endpoint p95 < 150 ms locally against `scripts/seed_dev.py` data (DoD #12, ROADMAP §7 item 12).

---

## 10. Everything dynamic (TD-13)

`CenterSettings` — tenant-schema singleton (lands D1-B; lives with org/tenant config, exposed via one settings endpoint) — holds every per-school knob: grading scheme, late threshold minutes, attendance correction window, assignment grace, file size cap, allowed file types, currency, quiet hours, OTP channel prefs, `open_registration` (TD-17).

**If a school could plausibly want a number different, it is a `CenterSettings` field, not a constant.** Adding a knob:

1. Add the field with a sane default (`late_threshold_minutes = models.PositiveSmallIntegerField(default=10)`).
2. Migration (one logical change).
3. It appears automatically in the settings endpoint serializer (keep that serializer explicit-fields, not `__all__`).
4. Consume via the cached accessor — never query the table directly in hot paths:

```python
# accessor (D1-B ships it; pattern shown for consumers)
def get_center_settings() -> CenterSettings:
    key = f"center_settings:{connection.schema_name}"
    obj = cache.get(key)
    if obj is None:
        obj, _ = CenterSettings.objects.get_or_create(pk=1)
        cache.set(key, obj, timeout=300)
    return obj
# post_save receiver on CenterSettings deletes the key — never stale > one save
```

Grep test before commit: any literal like `10`, `200 * 1024 * 1024`, `"UZS"` in a service is a smell — either it is a `CenterSettings` field or a named `settings.py` constant with a comment saying why it is platform-level (like `OTP_LENGTH` in `config/settings/base.py`).

---

## 11. Security do / don't

| DO | DON'T |
|---|---|
| Secrets via `environ.Env` in `config/settings/base.py`; new ones get an env default | Hardcode any credential, token, sender ID (TD-17: Eskiz `"4546"` is a bug being fixed) |
| Encrypt at rest with TD-11 fields: `national_id`, `medical_notes`, provider credentials (`ProviderConfig`), Soliq tokens, push-token stores | Put PII in plain `CharField`s or in log lines |
| Validate at serializer (shape) AND service (business rules) — both layers, always | Trust `request.data` in a service, or skip service validation "because the serializer did it" |
| File uploads: libmagic content-type check, extension+MIME allowlist from `CenterSettings`, size cap (TASKS §13, §23) | Trust the client `Content-Type` or filename |
| ORM only; `extra()`/`raw()` require a WORKLOG justification | String-build SQL, ever |
| Hash codes/tokens before storing (see `OTP.code_hash`, `make_password` in `apps/auth/services.py`) | Store raw OTPs, raw refresh tokens, raw API keys |
| Audit-log: logins, OTP events, permission grants, impersonation, grade changes, invoice/payment mutations, exports, ProviderConfig changes (TD-9) | Let a sensitive mutation land without an audit row |
| Verify webhook signatures before touching any row (§3 item 5) | Process first, verify later |
| Time everything with `timezone.now()` (`USE_TZ=True`, `Asia/Tashkent`) | `datetime.now()` / naive datetimes |

---

## 12. Pitfalls — wrong way → right way

| # | Wrong | Right |
|---|---|---|
| 1 | Querying tenant models on public schema (plain `ModelViewSet`) | Inherit `TenantSafeModelViewSet`; webhooks use the TD-6 `schema_context` pattern |
| 2 | `students.StudentProfile` FK → `teachers.TeacherProfile` | Route via `cohorts`/`attendance`/`academics` (`docs/adding-an-app.md`) |
| 3 | `requests.post(eskiz...)` inside a view or service | Celery task with retry/backoff/idempotency (§6) |
| 4 | `required_perm = "attendance:read"` flat string (scaffold style) | `resource` + `required_perms` per-action dict (TD-5, §4) |
| 5 | `fields = "__all__"` on a model with `national_id`/credentials | Explicit field tuples; separate read/write serializers (DoD #4) |
| 6 | `datetime.now()`, naive datetimes | `django.utils.timezone.now()`; `USE_TZ=True`, TZ `Asia/Tashkent` |
| 7 | Tests hitting models without a tenant schema active | Use the two-tenant fixtures from `agents/TESTING.md` (D1-E); never `--keepdb` hacks around it |
| 8 | Ending a session without logging | Append your `agents/WORKLOG.md` entry — mandatory (ROADMAP §2.2) |
| 9 | "Fixing" another lane's migration or reordering shared files | Never edit merged migrations; `makemigrations --merge` for conflicts; shared files are additive-only |
| 10 | Hardcoding a model name like the README's `claude-opus-4-7` vs settings' `claude-sonnet-4-6` (TD-17 bug) | Read `settings.ANTHROPIC_DEFAULT_MODEL` — one source of truth, in settings |
| 11 | School-policy literal in code (`grace = 15`) | `CenterSettings` field + cached accessor (TD-13, §10) |
| 12 | Calling `get_sms_client()` from a domain app | Emit a signal; `apps/notifications` dispatches (§7) |
| 13 | Error dict built by hand in a view | Raise a `StarforgeError` subclass; the handler builds the TD-18 envelope |
| 14 | Unpaginated list / `.all()` in a loop | `DefaultPagination`/`TimelinePagination` + selector with eager loading (§8, §9) |

---

## 13. Daily quality bar — pre-commit checklist (DoD, ROADMAP §3)

Run through this literally before every merge to `master`:

- [ ] **1. Models** — constraints (`UniqueConstraint`/`CheckConstraint`), `db_index` on every FK + every selector-filtered field, `__str__`, `Meta.ordering`; migration generated, inspected, committed.
- [ ] **2. TD-13** — zero magic numbers; school-variable knobs read from `CenterSettings`.
- [ ] **3. Services & selectors** — writes via typed, transactional `services.py`; reads via `selectors.py` with `select_related`/`prefetch_related`.
- [ ] **4. Serializers** — read/write split where shapes differ; no `fields = "__all__"` near sensitive fields.
- [ ] **5. Views** — `TenantSafeModelViewSet`, per-action perms (TD-5), `object_scope` where scoped, django-filter + search + ordering on lists.
- [ ] **6. URLs** — registered in app `urls.py`, included in `config/urls.py`.
- [ ] **7. OpenAPI** — `@extend_schema` with summary, tags, examples, error responses; schema job green.
- [ ] **8. Signals/events** — user-facing happenings emit signals to `apps/notifications`; sensitive mutations audited (TD-9); no direct adapter calls.
- [ ] **9. Async** — all external-service work in Celery with retries + idempotency key.
- [ ] **10. Tests** — happy path, permission-denied per role, cross-tenant isolation, validation edges, query-count assertion on lists (`agents/TESTING.md`); shipped in the same branch.
- [ ] **11. i18n** — `gettext_lazy` on every user-facing string; templates in uz/ru/en.
- [ ] **12. Speed** — lists paginated, p95 < 150 ms on seeded data, no unindexed filter.
- [ ] **13. Bookkeeping** — `TASKS.md` ticked, `agents/WORKLOG.md` entry appended, `docs/` updated if behavior diverged.

Gate commands (all green before push — ROADMAP §2.2):

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy apps core infrastructure config
uv run pytest -q   # coverage floor: 70% D1 → 80% D3 → 85% D5 (TD-20)
```
