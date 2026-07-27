"""D3-F-1/2/3 — webhook signature tampering, replay protection, wrong-tenant slug.

Adversarial coverage of the public-schema webhook intake (TD-6):
``POST /api/v1/webhooks/<provider>/<center_slug>/``. The webhook is posted to
the apex/public host; the view resolves the tenant by slug, enters
``schema_context``, loads that tenant's ProviderConfig, and verifies the
provider signature BEFORE touching any row (CODE-GUIDE §3 item 5).

F-1 signature tampering:
  - Click bad sign -> error ``-1``, zero Payment rows.
  - Payme wrong HTTP Basic -> ``-32504``, HTTP 200, JSON-RPC error member.
  - Uzum bad HMAC -> rejected, ``WebhookEvent.status == "rejected"``.

F-2 replay protection:
  - Same Payme ``CreateTransaction`` id twice -> identical response, ONE Payment.
  - Same Click ``click_trans_id`` complete twice -> second recorded as
    ``duplicate``; allocation runs exactly once (assert allocation row count).

F-3 wrong-tenant slug:
  - A signature valid for center A posted to center B's slug fails against B's
    ProviderConfig (Payme: account error in -31050..-31099); no rows either side.
  - A nonexistent slug -> 404 envelope.

Lane B builds the clients/views in parallel; lane imports are lazy. The
orchestrator runs this on Postgres after A..E merge.
"""

from __future__ import annotations

import pytest

from apps.payments.tests import _helpers as helpers
from apps.payments.tests import builders as bld

pytestmark = pytest.mark.django_db

AMOUNT_UZS = "150000.00"
AMOUNT_TIYIN = 15_000_000
ACCOUNT = {"order_id": "INV-2026-000001"}


@pytest.fixture(autouse=True)
def _map_testserver_to_public(public_tenant):
    """Webhooks are posted to the apex/``testserver`` host (public schema, TD-6).
    django-tenants only routes ``testserver`` to the public schema when a Domain
    row maps it — the root ``public_tenant`` fixture creates that mapping. Without
    it the POST 404s (no tenant for the host), not a view bug."""
    return public_tenant


@pytest.fixture
def configured_a(tenant_a):
    helpers.seed_provider_configs(tenant_a)
    helpers.seed_open_invoice(tenant_a, number=ACCOUNT["order_id"], amount_uzs=AMOUNT_UZS)
    return tenant_a


@pytest.fixture
def configured_b(tenant_b):
    helpers.seed_provider_configs(tenant_b)
    # tenant_b has its OWN invoice numbering; A's INV-2026-000001 must not resolve.
    helpers.seed_open_invoice(tenant_b, number="INV-2026-000777", amount_uzs=AMOUNT_UZS)
    return tenant_b


def _post_payme(center, payload, *, wrong_auth=False):
    return helpers.public_client().post(
        helpers.webhook_url("payme", center.schema_name),
        data=payload,
        format="json",
        **bld.payme_auth_headers(wrong=wrong_auth),
    )


def _post_click(center, payload):
    return helpers.public_client().post(
        helpers.webhook_url("click", center.schema_name), data=payload, format="json"
    )


def _post_uzum(center, body, headers):
    return helpers.public_client().post(
        helpers.webhook_url("uzum", center.schema_name), data=body, format="json", **headers
    )


# --------------------------------------------------------------------------- #
# D3-F-1 — signature tampering
# --------------------------------------------------------------------------- #
def test_click_invalid_signature_rejected_minus_1_zero_rows(configured_a):
    """Click: flipped one char in sign_string -> error -1, zero Payment rows."""
    payload = bld.make_click_complete(
        merchant_trans_id=ACCOUNT["order_id"], amount=AMOUNT_UZS, tamper_sign=True
    )
    resp = _post_click(configured_a, payload)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    # Click's own error envelope: error == -1 on bad sign.
    assert body.get("error") == -1, body
    assert helpers.payment_rows(configured_a) == []


def test_payme_wrong_basic_auth_minus_32504_http200(configured_a):
    """Payme: wrong Basic auth -> -32504, HTTP 200, JSON-RPC error member."""
    payload = bld.payme_check_perform(amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
    resp = _post_payme(configured_a, payload, wrong_auth=True)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32504
    assert "result" not in body
    assert helpers.payment_rows(configured_a) == []


def test_uzum_bad_hmac_rejected_webhookevent(configured_a):
    """Uzum: bad HMAC -> rejected; a WebhookEvent row records status=rejected."""
    body, headers = bld.make_uzum_webhook(order_id=ACCOUNT["order_id"], amount=AMOUNT_UZS, tamper_sign=True)
    resp = _post_uzum(configured_a, body, headers)
    # Uzum/Click use the standard TD-18 envelope on errors (Lane B decision).
    assert resp.status_code in (400, 401, 403), resp.content
    events = helpers.webhook_event_rows(configured_a, provider="uzum")
    assert events, "a WebhookEvent should be recorded even for a rejected webhook"
    assert any(e.status == "rejected" and e.signature_valid is False for e in events)
    assert helpers.payment_rows(configured_a) == []


def test_uzum_valid_hmac_accepted(configured_a):
    """Control: a correctly-signed Uzum webhook is accepted (signature_valid)."""
    body, headers = bld.make_uzum_webhook(order_id=ACCOUNT["order_id"], amount=AMOUNT_UZS)
    resp = _post_uzum(configured_a, body, headers)
    assert resp.status_code in (200, 201, 202), resp.content
    events = helpers.webhook_event_rows(configured_a, provider="uzum", event_id="uzum-evt-1")
    assert events
    assert events[0].signature_valid is True


# --------------------------------------------------------------------------- #
# D3-F-2 — replay protection
# --------------------------------------------------------------------------- #
def test_payme_create_transaction_replay_one_payment(configured_a):
    """Same Payme CreateTransaction id twice -> identical response, one Payment."""
    payload = bld.payme_create_transaction(
        payme_id="replay-create-1", amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT
    )
    first = _post_payme(configured_a, payload).json()
    second = _post_payme(configured_a, payload).json()
    assert "result" in first
    assert "result" in second
    assert second["result"]["create_time"] == first["result"]["create_time"]
    assert second["result"]["state"] == 1
    rows = helpers.payment_rows(configured_a, provider_txn_id="replay-create-1")
    assert len(rows) == 1


def test_click_complete_replay_duplicate_single_allocation(configured_a):
    """Same Click click_trans_id complete twice -> second recorded duplicate;
    allocation runs exactly once."""
    prepare = bld.make_click_prepare(merchant_trans_id=ACCOUNT["order_id"], amount=AMOUNT_UZS)
    _post_click(configured_a, prepare)
    complete = bld.make_click_complete(
        click_trans_id=prepare["click_trans_id"],
        merchant_trans_id=ACCOUNT["order_id"],
        amount=AMOUNT_UZS,
    )
    first = _post_click(configured_a, complete)
    assert first.status_code == 200, first.content
    second = _post_click(configured_a, complete)
    assert second.status_code == 200, second.content

    # exactly one completed Payment, and exactly one allocation for it
    pays = helpers.payment_rows(configured_a, provider="click", provider_txn_id=prepare["click_trans_id"])
    assert len(pays) == 1
    allocs = helpers.allocation_rows(configured_a, payment_id=pays[0].id)
    assert len(allocs) == 1
    # the replay is recorded as a duplicate WebhookEvent
    events = helpers.webhook_event_rows(configured_a, provider="click")
    assert any(e.status == "duplicate" for e in events)


# --------------------------------------------------------------------------- #
# Regression (round-2 bug hunt): a Complete callback must NEVER be ACKed as
# success + marked processed unless a Payment was actually recorded — otherwise
# the provider stops retrying and a captured payment is silently lost.
# --------------------------------------------------------------------------- #
def test_click_complete_unknown_invoice_is_rejected_not_acked(configured_a):
    """R2-09: a validly-signed Click Complete whose merchant_trans_id resolves to
    no invoice must be REJECTED (retryable), not marked processed + success."""
    complete = bld.make_click_complete(
        click_trans_id="click-unknown-1",
        merchant_trans_id="INV-DOES-NOT-EXIST",
        amount=AMOUNT_UZS,
    )
    resp = _post_click(configured_a, complete)
    body = resp.json()
    assert body["error"] != 0, body  # NOT the success code
    # No payment created, and the event is retryable (rejected), not processed.
    assert helpers.payment_rows(configured_a, provider="click") == []
    events = helpers.webhook_event_rows(configured_a, provider="click")
    assert events
    assert all(e.status == "rejected" for e in events)


def test_click_complete_processing_error_is_rejected_not_swallowed(configured_a, monkeypatch):
    """R2-02: if processing throws a non-ValidationException (e.g. a transient DB
    error), the event must be REJECTED so the provider's retry reprocesses — never
    left as RECEIVED (which the next attempt would dedup-swallow as DUPLICATE)."""
    from apps.payments import services

    def _boom(*a, **k):
        raise RuntimeError("simulated deadlock")

    monkeypatch.setattr(services, "process_click_complete", _boom)
    complete = bld.make_click_complete(
        click_trans_id="click-boom-1", merchant_trans_id=ACCOUNT["order_id"], amount=AMOUNT_UZS
    )
    resp = _post_click(configured_a, complete)
    assert resp.json()["error"] != 0
    events = helpers.webhook_event_rows(configured_a, provider="click", event_id="click-boom-1:1")
    assert events
    assert events[0].status == "rejected"
    assert helpers.payment_rows(configured_a, provider="click") == []


# --------------------------------------------------------------------------- #
# D3-F-3 — wrong-tenant slug
# --------------------------------------------------------------------------- #
def test_payme_valid_for_a_posted_to_b_fails_no_rows(configured_a, configured_b):
    """A signature/account valid for center A, posted to center B's slug, must
    fail against B's ProviderConfig + invoices (account error band); no rows in
    either schema."""
    payload = bld.payme_create_transaction(
        payme_id="cross-slug-1", amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT
    )
    resp = _post_payme(configured_b, payload)  # A's invoice number, B's slug
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert "error" in body
    code = body["error"]["code"]
    # A's INV number does not exist in B -> account error band -31050..-31099.
    assert -31099 <= code <= -31050
    assert helpers.payment_rows(configured_a) == []
    assert helpers.payment_rows(configured_b) == []


def test_nonexistent_slug_404(configured_a):
    """A webhook for an unknown center slug -> 404 envelope (TD-6)."""
    payload = bld.payme_check_perform(amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
    resp = helpers.public_client().post(
        helpers.webhook_url("payme", "no_such_center"),
        data=payload,
        format="json",
        **bld.payme_auth_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_inactive_center_slug_404(configured_a):
    """An inactive center resolves to 404 (the slug lookup filters is_active)."""
    from apps.tenancy.models import Center

    Center.objects.filter(schema_name=configured_a.schema_name).update(is_active=False)
    try:
        payload = bld.payme_check_perform(amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)
        resp = _post_payme(configured_a, payload)
        assert resp.status_code == 404
    finally:
        Center.objects.filter(schema_name=configured_a.schema_name).update(is_active=True)


# --------------------------------------------------------------------------- #
# R6/CONF3 — storage-exhaustion DoS: throttle ONLY the invalid-signature path
# (a valid provider callback is never touched), plus WebhookEvent retention.
# --------------------------------------------------------------------------- #
def test_invalid_webhook_flood_is_capped_per_ip(configured_a, monkeypatch):
    """A forged-signature flood from one IP stops inserting WebhookEvent rows once the
    per-IP invalid-webhook budget is spent — bounding the storage DoS — while still
    recording the first few for audit."""
    from django.core.cache import cache

    from apps.payments import webhook_views

    cache.clear()  # a clean per-IP bucket (LocMem cache persists across tests)
    monkeypatch.setattr(webhook_views, "WEBHOOK_INVALID_RATELIMIT", 2)

    for i in range(5):
        body, headers = bld.make_uzum_webhook(
            event_id=f"flood-{i}", order_id=ACCOUNT["order_id"], amount=AMOUNT_UZS, tamper_sign=True
        )
        _post_uzum(configured_a, body, headers)

    events = helpers.webhook_event_rows(configured_a, provider="uzum")
    assert len(events) == 2  # only the first 2 forged webhooks recorded; the rest dropped pre-INSERT
    assert all(e.status == "rejected" for e in events)


def test_valid_webhook_is_never_throttled(configured_a, monkeypatch):
    """The invalid-path throttle must never touch a validly-signed callback (the money
    path can't be re-broken — the reason R4-02 removed the blanket webhook limit). Even
    after the invalid budget is exhausted for this IP, a valid webhook is recorded + acked."""
    from django.core.cache import cache

    from apps.payments import webhook_views

    cache.clear()
    monkeypatch.setattr(webhook_views, "WEBHOOK_INVALID_RATELIMIT", 1)

    for i in range(3):  # exhaust the invalid budget for this IP
        body, headers = bld.make_uzum_webhook(
            event_id=f"bad-{i}", order_id=ACCOUNT["order_id"], amount=AMOUNT_UZS, tamper_sign=True
        )
        _post_uzum(configured_a, body, headers)

    body, headers = bld.make_uzum_webhook(event_id="good-1", order_id=ACCOUNT["order_id"], amount=AMOUNT_UZS)
    resp = _post_uzum(configured_a, body, headers)
    assert resp.status_code in (200, 201, 202), resp.content
    events = helpers.webhook_event_rows(configured_a, provider="uzum", event_id="good-1")
    assert events
    assert events[0].signature_valid is True


def test_prune_webhook_events_removes_old_keeps_recent(configured_a):
    """The retention beat sweep deletes WebhookEvent rows past the window and keeps recent
    ones — bounding long-term growth even under a distributed flood."""
    from datetime import timedelta

    from django.utils import timezone
    from django_tenants.utils import schema_context

    from apps.payments.models import Provider, WebhookEvent
    from celery_tasks.payment_tasks import WEBHOOK_RETENTION_DAYS, prune_webhook_events_for_schema

    with schema_context(configured_a.schema_name):
        old = WebhookEvent.objects.create(provider=Provider.UZUM, event_id="old-1")
        recent = WebhookEvent.objects.create(provider=Provider.UZUM, event_id="recent-1")
        WebhookEvent.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=WEBHOOK_RETENTION_DAYS + 5)
        )
        deleted = prune_webhook_events_for_schema()
        assert deleted == 1
        assert not WebhookEvent.objects.filter(pk=old.pk).exists()
        assert WebhookEvent.objects.filter(pk=recent.pk).exists()
