from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlencode

import pytest
from django.test import override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.payments.tests import _helpers as helpers
from apps.payments.tests import builders

pytestmark = pytest.mark.django_db

AMOUNT_UZS = "150000.00"
AMOUNT_TIYIN = 15_000_000
ACCOUNT = {"order_id": "INV-SEC-1"}


@pytest.fixture(autouse=True)
def _public_host(public_tenant):
    return public_tenant


@pytest.fixture
def configured(tenant_a):
    helpers.seed_provider_configs(tenant_a)
    invoice = helpers.seed_open_invoice(
        tenant_a,
        number=ACCOUNT["order_id"],
        amount_uzs=AMOUNT_UZS,
    )
    return tenant_a, invoice


def _raw_post(center, provider: str, raw: bytes | str, content_type: str, **headers):
    return helpers.public_client().generic(
        "POST",
        helpers.webhook_url(provider, center.schema_name),
        data=raw,
        content_type=content_type,
        **headers,
    )


def test_webhook_body_limit_rejects_before_audit_insert(configured):
    center, _invoice = configured
    response = _raw_post(
        center,
        "click",
        b"x" * (64 * 1024 + 1),
        "application/json",
    )

    assert response.json()["error"] != 0
    assert helpers.webhook_event_rows(center) == []


def test_payme_duplicate_json_keys_are_rejected_as_json_rpc(configured):
    center, _invoice = configured
    response = _raw_post(
        center,
        "payme",
        b'{"id":1,"id":2,"method":"CheckTransaction","params":{"id":"x"}}',
        "application/json",
        HTTP_AUTHORIZATION=builders.payme_basic_auth(),
    )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32700


def test_click_accepts_provider_form_encoding_and_returns_prepare_id(configured):
    center, invoice = configured
    payload = builders.make_click_prepare(
        merchant_trans_id=invoice.number,
        amount=AMOUNT_UZS,
    )
    response = _raw_post(
        center,
        "click",
        urlencode(payload),
        "application/x-www-form-urlencoded",
    )

    assert response.status_code == 200
    assert response.json()["error"] == 0
    assert response.json()["merchant_prepare_id"] == invoice.pk


def test_click_failed_complete_never_credits_and_corrected_retry_can_recover(configured):
    center, invoice = configured
    prepare = builders.make_click_prepare(
        click_trans_id="click-provider-failure",
        merchant_trans_id=invoice.number,
        amount=AMOUNT_UZS,
    )
    prepared = _raw_post(center, "click", urlencode(prepare), "application/x-www-form-urlencoded")
    assert prepared.json()["error"] == 0

    failed = builders.make_click_complete(
        click_trans_id="click-provider-failure",
        merchant_trans_id=invoice.number,
        merchant_prepare_id=str(invoice.pk),
        amount=AMOUNT_UZS,
        error=-5017,
    )
    response = _raw_post(center, "click", urlencode(failed), "application/x-www-form-urlencoded")

    assert response.json()["error"] == -9
    assert helpers.payment_rows(center) == []
    assert helpers.allocation_rows(center, invoice_id=invoice.pk) == []

    corrected = {**failed, "error": 0}
    recovered = _raw_post(center, "click", urlencode(corrected), "application/x-www-form-urlencoded")
    payment = helpers.payment_rows(center, provider_txn_id="click-provider-failure")[0]
    assert recovered.json()["error"] == 0
    assert recovered.json()["merchant_confirm_id"] == payment.pk
    assert len(helpers.allocation_rows(center, invoice_id=invoice.pk)) == 1


def test_rejected_click_nonce_cannot_be_rebound_to_a_different_intent(configured):
    center, invoice = configured
    failed = builders.make_click_complete(
        click_trans_id="click-rejected-intent",
        merchant_trans_id=invoice.number,
        merchant_prepare_id=str(invoice.pk),
        amount=AMOUNT_UZS,
        error=-5017,
    )
    rejected = helpers.public_client().post(
        helpers.webhook_url("click", center.schema_name), data=failed, format="json"
    )
    rebound = builders.make_click_complete(
        click_trans_id="click-rejected-intent",
        merchant_trans_id=invoice.number,
        merchant_prepare_id=str(invoice.pk),
        amount="1",
        error=0,
    )
    conflict = helpers.public_client().post(
        helpers.webhook_url("click", center.schema_name), data=rebound, format="json"
    )

    assert rejected.json()["error"] == -9
    assert conflict.json()["error"] == -8
    assert helpers.payment_rows(center, provider_txn_id="click-rejected-intent") == []


def test_webhook_audit_retains_only_keyed_fingerprint_not_body_or_ip(configured):
    center, invoice = configured
    body, headers = builders.make_uzum_webhook(
        event_id="privacy-1",
        order_id=invoice.number,
        amount=AMOUNT_UZS,
        status="PENDING",
    )
    response = helpers.public_client().post(
        helpers.webhook_url("uzum", center.schema_name),
        data=body,
        format="json",
        REMOTE_ADDR="203.0.113.25",
        **headers,
    )

    assert response.status_code == 400
    event = helpers.webhook_event_rows(center, provider="uzum", event_id="privacy-1")[0]
    assert not hasattr(event, "remote_ip")
    assert set(event.payload) == {"fingerprint_hmac_sha256"}
    assert len(event.payload["fingerprint_hmac_sha256"]) == 64
    assert invoice.number not in str(event.payload)


def test_click_event_id_reuse_with_different_intent_is_not_acknowledged(configured):
    center, invoice = configured
    prepare = builders.make_click_prepare(
        click_trans_id="reuse-click-1",
        merchant_trans_id=invoice.number,
        amount=AMOUNT_UZS,
    )
    prepared = helpers.public_client().post(
        helpers.webhook_url("click", center.schema_name), data=prepare, format="json"
    )
    prepare_id = str(prepared.json()["merchant_prepare_id"])
    complete = builders.make_click_complete(
        click_trans_id="reuse-click-1",
        merchant_trans_id=invoice.number,
        merchant_prepare_id=prepare_id,
        amount=AMOUNT_UZS,
    )
    first = helpers.public_client().post(
        helpers.webhook_url("click", center.schema_name), data=complete, format="json"
    )
    conflicting = builders.make_click_complete(
        click_trans_id="reuse-click-1",
        merchant_trans_id=invoice.number,
        merchant_prepare_id=prepare_id,
        amount="1",
    )
    second = helpers.public_client().post(
        helpers.webhook_url("click", center.schema_name), data=conflicting, format="json"
    )

    assert first.json()["error"] == 0
    assert second.json()["error"] != 0
    assert len(helpers.payment_rows(center, provider_txn_id="reuse-click-1")) == 1


def test_click_inflight_duplicate_is_retryable_not_false_success(configured):
    center, invoice = configured
    from apps.payments import services
    from apps.payments.models import Provider, WebhookEvent

    complete = builders.make_click_complete(
        click_trans_id="inflight-click-1",
        merchant_trans_id=invoice.number,
        merchant_prepare_id=str(invoice.pk),
        amount=AMOUNT_UZS,
    )
    with schema_context(center.schema_name):
        fingerprint = services.webhook_payload_fingerprint(provider=Provider.CLICK, payload=complete)
        WebhookEvent.objects.create(
            provider=Provider.CLICK,
            event_id="inflight-click-1:1",
            signature_valid=True,
            status=WebhookEvent.Status.RECEIVED,
            payload={"fingerprint_hmac_sha256": fingerprint},
        )
    response = helpers.public_client().post(
        helpers.webhook_url("click", center.schema_name), data=complete, format="json"
    )

    assert response.json()["error"] != 0
    assert helpers.payment_rows(center, provider="click") == []


def test_stale_received_event_is_reclaimed_after_processing_lease(configured):
    center, invoice = configured
    from datetime import timedelta

    from apps.payments import services
    from apps.payments.models import Provider, WebhookEvent

    complete = builders.make_click_complete(
        click_trans_id="stale-click-1",
        merchant_trans_id=invoice.number,
        merchant_prepare_id=str(invoice.pk),
        amount=AMOUNT_UZS,
    )
    with schema_context(center.schema_name):
        fingerprint = services.webhook_payload_fingerprint(provider=Provider.CLICK, payload=complete)
        WebhookEvent.objects.create(
            provider=Provider.CLICK,
            event_id="stale-click-1:1",
            signature_valid=True,
            status=WebhookEvent.Status.RECEIVED,
            payload={"fingerprint_hmac_sha256": fingerprint},
            last_attempted_at=timezone.now() - timedelta(minutes=6),
        )
    response = helpers.public_client().post(
        helpers.webhook_url("click", center.schema_name), data=complete, format="json"
    )

    assert response.json()["error"] == 0
    assert len(helpers.payment_rows(center, provider_txn_id="stale-click-1")) == 1


def test_payme_create_replay_must_match_original_time_amount_and_account(configured):
    center, _invoice = configured
    original = builders.payme_create_transaction(
        payme_id="payme-intent-1",
        amount_tiyin=AMOUNT_TIYIN,
        account=ACCOUNT,
        time_ms=1_700_000_000_000,
    )
    changed = builders.payme_create_transaction(
        payme_id="payme-intent-1",
        amount_tiyin=AMOUNT_TIYIN,
        account=ACCOUNT,
        time_ms=1_700_000_000_001,
    )
    first = helpers.public_client().post(
        helpers.webhook_url("payme", center.schema_name),
        data=original,
        format="json",
        **builders.payme_auth_headers(),
    )
    second = helpers.public_client().post(
        helpers.webhook_url("payme", center.schema_name),
        data=changed,
        format="json",
        **builders.payme_auth_headers(),
    )

    assert "result" in first.json()
    assert "error" in second.json()
    assert len(helpers.payment_rows(center, provider_txn_id="payme-intent-1")) == 1


def test_payme_cannot_charge_a_draft_or_void_invoice(configured):
    center, invoice = configured
    with schema_context(center.schema_name):
        invoice.status = "void"
        invoice.save(update_fields=["status", "updated_at"])
    payload = builders.payme_check_perform(amount_tiyin=AMOUNT_TIYIN, account=ACCOUNT)

    response = helpers.public_client().post(
        helpers.webhook_url("payme", center.schema_name),
        data=payload,
        format="json",
        HTTP_AUTHORIZATION=builders.payme_basic_auth(),
    )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -31008
    assert helpers.payment_rows(center) == []


def test_signed_legacy_uzum_non_paid_event_never_credits_invoice(configured):
    center, invoice = configured
    body, headers = builders.make_uzum_webhook(
        event_id="uzum-pending-1",
        order_id=invoice.number,
        amount=AMOUNT_UZS,
        status="PENDING",
    )
    response = helpers.public_client().post(
        helpers.webhook_url("uzum", center.schema_name),
        data=body,
        format="json",
        **headers,
    )

    assert response.status_code == 400
    assert helpers.payment_rows(center, provider="uzum") == []
    event = helpers.webhook_event_rows(center, provider="uzum", event_id="uzum-pending-1")[0]
    assert event.status == "rejected"


@override_settings(UZUM_LEGACY_INTEGRATION_ENABLED=False)
def test_legacy_uzum_route_fails_closed_when_disabled(configured):
    center, invoice = configured
    body, headers = builders.make_uzum_webhook(order_id=invoice.number, amount=AMOUNT_UZS)
    response = helpers.public_client().post(
        helpers.webhook_url("uzum", center.schema_name),
        data=body,
        format="json",
        **headers,
    )

    assert response.status_code == 503
    assert helpers.payment_rows(center, provider="uzum") == []


def test_checkout_charges_only_outstanding_and_callback_claims_same_row(configured):
    center, invoice = configured
    from apps.finance.models import PaymentAllocation
    from apps.payments import services
    from apps.payments.models import Payment

    with schema_context(center.schema_name):
        PaymentAllocation.objects.create(
            invoice=invoice,
            payment_id=999_999,
            amount_uzs=Decimal("50000.00"),
        )
        checkout = services.create_checkout(
            invoice_id=invoice.pk,
            provider="click",
            idempotency_key="partial-checkout-1",
        )
        pending = Payment.objects.get(pk=checkout["payment_id"])
        assert pending.amount_uzs == Decimal("100000.00")
        completed = services.process_click_complete(
            payload={
                "click_trans_id": "partial-click-1",
                "merchant_trans_id": invoice.number,
                "amount": "100000",
            },
            invoice=invoice,
        )

        assert completed.pk == pending.pk
        assert Payment.objects.filter(provider=Payment.Method.CLICK).count() == 1


def test_second_checkout_key_does_not_create_an_ambiguous_pending_intent(configured):
    center, invoice = configured
    from apps.payments import services
    from apps.payments.models import Payment
    from core.exceptions import ConflictException

    with schema_context(center.schema_name):
        services.create_checkout(
            invoice_id=invoice.pk,
            provider="click",
            idempotency_key="checkout-first",
        )
        with pytest.raises(ConflictException) as exc:
            services.create_checkout(
                invoice_id=invoice.pk,
                provider="click",
                idempotency_key="checkout-second",
            )
        assert exc.value.code == "payment_intent_in_progress"
        assert Payment.objects.filter(provider=Payment.Method.CLICK).count() == 1


def test_provider_transaction_uniqueness_targets_external_providers_only(configured):
    center, invoice = configured
    from django.db import IntegrityError, transaction

    from apps.payments.models import Payment

    common = {
        "amount_uzs": Decimal("1.00"),
        "branch_at_payment_id": invoice.branch_at_issue_id,
        "department_at_payment_id": invoice.department_at_issue_id,
        "attribution_status": "captured",
    }
    with schema_context(center.schema_name):
        Payment.objects.create(
            provider=Payment.Method.CASH,
            provider_txn_id="cash:shared-shift",
            idempotency_key="cash-provider-id-scope-1",
            **common,
        )
        Payment.objects.create(
            provider=Payment.Method.CASH,
            provider_txn_id="cash:shared-shift",
            idempotency_key="cash-provider-id-scope-2",
            **common,
        )
        Payment.objects.create(
            provider=Payment.Method.CLICK,
            provider_txn_id="external-unique-1",
            idempotency_key="external-provider-id-scope-1",
            **common,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Payment.objects.create(
                provider=Payment.Method.CLICK,
                provider_txn_id="external-unique-1",
                idempotency_key="external-provider-id-scope-2",
                **common,
            )
