"""Trust-boundary regressions for Soliq responses and receipt downloads."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.payments.tests import _helpers as helpers

pytestmark = pytest.mark.django_db


def _confirmed_receipt(center, *, number: str, payload=None, pdf_key: str = ""):
    from apps.payments.models import FiscalReceipt, Payment

    invoice = helpers.seed_open_invoice(center, number=number, amount_uzs="100.00")
    with schema_context(center.schema_name):
        payment = Payment.objects.create(
            provider=Payment.Method.PAYME,
            amount_uzs=Decimal("100.00"),
            status=Payment.Status.COMPLETED,
            idempotency_key=f"receipt-{number}",
            account_ref=invoice.number,
            branch_at_payment_id=invoice.branch_at_issue_id,
            department_at_payment_id=invoice.department_at_issue_id,
            attribution_status=invoice.attribution_status,
            paid_at=timezone.now(),
        )
        receipt = FiscalReceipt.objects.create(
            payment=payment,
            status=FiscalReceipt.Status.CONFIRMED,
            fiscal_sign="fiscal-sign",
            payload=payload or {},
            pdf_key=pdf_key,
        )
    return invoice, payment, receipt


@override_settings(
    FISCALIZATION_ENABLED=True,
    SOLIQ_QR_ALLOWED_HOSTS=["ofd.soliq.uz"],
)
def test_malicious_provider_response_is_sanitized_and_cannot_supply_storage_key(
    tenant_a,
    monkeypatch,
):
    from apps.payments import services
    from apps.payments.models import FiscalReceipt, Payment

    invoice = helpers.seed_open_invoice(
        tenant_a,
        number="INV-FISCAL-MALICIOUS-1",
        amount_uzs="100.00",
    )

    class MaliciousClient:
        def fiscalize(self, **_kwargs):
            return {
                "fiscal_sign": "safe-provider-sign",
                "qr_url": "https://ofd.soliq.uz.evil.example/check?secret=1",
                "raw": {
                    "receipt_id": "provider-receipt-1",
                    "timestamp": "2026-08-02T10:00:00+05:00",
                    "mock": False,
                    "payment_id": payment.pk,
                    "terminal_id": "x" * 257,
                    "pdf_key": "another_tenant/receipts/1.pdf",
                    "token": "provider-secret-must-not-persist",
                    "items": [{"private": "customer-data"}],
                },
            }

    monkeypatch.setattr("infrastructure.fiscal.get_fiscal_client", lambda: MaliciousClient())
    with schema_context(tenant_a.schema_name):
        payment, _created = services.get_or_create_payment(
            idempotency_key="fiscal-malicious-provider-1",
            provider=Payment.Method.PAYME,
            amount_uzs=Decimal("100.00"),
            account_ref=invoice.number,
            invoice=invoice,
        )
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.COMPLETED)

        assert services.fiscalize_payment_body(payment.pk) == "safe-provider-sign"

        receipt = FiscalReceipt.objects.get(payment=payment)
        assert receipt.status == FiscalReceipt.Status.CONFIRMED
        assert receipt.qr_url == ""
        assert receipt.pdf_key == ""
        assert receipt.payload == {}
        assert receipt.provider_payload == {
            "receipt_id": "provider-receipt-1",
            "timestamp": "2026-08-02T10:00:00+05:00",
            "mock": False,
            "payment_id": payment.pk,
        }


def test_legacy_payload_pdf_key_is_never_presigned(
    tenant_a,
    user_in,
    as_user,
    monkeypatch,
):
    from apps.payments import services
    from core.permissions import Role

    invoice, payment, _receipt = _confirmed_receipt(
        tenant_a,
        number="INV-RECEIPT-LEGACY-1",
        payload={"pdf_key": "another_tenant/receipts/secret.pdf", "token": "secret"},
    )
    actor = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=invoice.branch_at_issue)
    client = as_user(tenant_a, actor)
    signed: list[str] = []
    queued: list[tuple[int, str]] = []
    monkeypatch.setattr(
        "apps.payments.views.v1.payment_views.presign_download",
        lambda key, *, expires_in: signed.append(key) or "https://storage.invalid/signed",
    )
    monkeypatch.setattr(
        services,
        "enqueue_receipt_pdf",
        lambda payment_id, schema: queued.append((payment_id, schema)),
    )

    assert client.head(f"/api/v1/payments/{payment.pk}/receipt/").status_code == 200
    assert signed == []
    assert queued == []

    response = client.get(f"/api/v1/payments/{payment.pk}/receipt/")
    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "not_generated",
        "generation_required": True,
    }
    assert signed == []
    assert queued == []

    generated = client.post(f"/api/v1/payments/{payment.pk}/receipt/", {}, format="json")
    assert generated.status_code == 202
    assert queued == [(payment.pk, tenant_a.schema_name)]


def test_cross_tenant_dedicated_pdf_key_is_never_presigned(
    tenant_a,
    user_in,
    as_user,
    monkeypatch,
):
    from apps.payments import services
    from core.permissions import Role

    invoice, payment, _receipt = _confirmed_receipt(
        tenant_a,
        number="INV-RECEIPT-CROSS-TENANT-1",
        pdf_key="another_tenant/receipts/1.pdf",
    )
    actor = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=invoice.branch_at_issue)
    client = as_user(tenant_a, actor)
    signed: list[str] = []
    queued: list[tuple[int, str]] = []
    monkeypatch.setattr(
        "apps.payments.views.v1.payment_views.presign_download",
        lambda key, *, expires_in: signed.append(key) or "https://storage.invalid/signed",
    )
    monkeypatch.setattr(
        services,
        "enqueue_receipt_pdf",
        lambda payment_id, schema: queued.append((payment_id, schema)),
    )

    response = client.get(f"/api/v1/payments/{payment.pk}/receipt/")
    assert response.status_code == 200
    assert signed == []
    assert queued == []

    generated = client.post(f"/api/v1/payments/{payment.pk}/receipt/", {}, format="json")
    assert generated.status_code == 202
    assert queued == [(payment.pk, tenant_a.schema_name)]


def test_only_exact_server_derived_pdf_key_is_presigned(
    tenant_a,
    user_in,
    as_user,
    monkeypatch,
):
    from apps.payments import services
    from apps.payments.models import FiscalReceipt
    from core.permissions import Role

    invoice, payment, receipt = _confirmed_receipt(
        tenant_a,
        number="INV-RECEIPT-EXACT-1",
    )
    with schema_context(tenant_a.schema_name):
        expected = services.receipt_pdf_key(payment.pk)
        FiscalReceipt.objects.filter(pk=receipt.pk).update(pdf_key=expected)

    actor = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=invoice.branch_at_issue)
    client = as_user(tenant_a, actor)
    signed: list[str] = []
    monkeypatch.setattr(
        "apps.payments.views.v1.payment_views.presign_download",
        lambda key, *, expires_in: signed.append(key) or "https://storage.invalid/signed",
    )

    response = client.get(f"/api/v1/payments/{payment.pk}/receipt/")
    assert response.status_code == 200
    assert response.json()["data"]["url"] == "https://storage.invalid/signed"
    assert signed == [expected]


def test_pdf_generation_ignores_legacy_and_invalid_keys_and_overwrites_exact_target(
    tenant_a,
    monkeypatch,
):
    from apps.payments import services

    _invoice, payment, receipt = _confirmed_receipt(
        tenant_a,
        number="INV-RECEIPT-GENERATE-1",
        payload={"pdf_key": "another_tenant/private.pdf", "token": "secret"},
        pdf_key="another_tenant/receipts/1.pdf",
    )
    uploads: list[tuple[str, bytes, str]] = []
    monkeypatch.setattr(services, "_render_receipt_pdf", lambda _payment, _receipt: b"%PDF-safe")
    monkeypatch.setattr(
        "infrastructure.storage.s3_client.upload_bytes",
        lambda key, data, *, content_type: uploads.append((key, data, content_type)) or key,
    )

    with schema_context(tenant_a.schema_name):
        expected = services.receipt_pdf_key(payment.pk)
        assert services.generate_receipt_pdf_body(payment.pk) == expected
        receipt.refresh_from_db()
        assert receipt.pdf_key == expected
        assert receipt.payload == {}
    assert uploads == [(expected, b"%PDF-safe", "application/pdf")]


def test_fiscal_failure_does_not_persist_exception_or_stale_untrusted_fields(tenant_a):
    from apps.payments import services
    from apps.payments.models import FiscalReceipt

    _invoice, payment, receipt = _confirmed_receipt(
        tenant_a,
        number="INV-RECEIPT-FAILURE-1",
        payload={"error": "old-secret", "pdf_key": "another_tenant/private.pdf"},
        pdf_key="another_tenant/receipts/1.pdf",
    )
    with schema_context(tenant_a.schema_name):
        FiscalReceipt.objects.filter(pk=receipt.pk).update(
            status=FiscalReceipt.Status.SUBMITTED,
            qr_url="https://evil.example/",
            provider_payload={"token": "old-secret"},
        )
        services.mark_fiscal_failed(payment.pk, RuntimeError("new-secret"))
        receipt.refresh_from_db()
        assert receipt.status == FiscalReceipt.Status.FAILED
        assert receipt.fiscal_sign == ""
        assert receipt.qr_url == ""
        assert receipt.provider_payload == {}
        assert receipt.pdf_key == ""
        assert receipt.payload == {}
