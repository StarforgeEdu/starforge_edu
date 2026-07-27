"""Payments domain models (D3-B-1).

Provider integrations (Click/Payme/Uzum), the payment ledger, webhook intake
records, and Soliq fiscal receipts. Credentials are encrypted at rest (TD-11);
``Payment.idempotency_key`` and ``WebhookEvent(provider, event_id)`` are the two
dedupe spines (D3-B-6). Cross-lane FK to ``finance.CashierShift`` is a STRING ref
(Lane B merges after Lane A) — no Python import of the finance app here.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from core.fields import EncryptedCharField


class Provider(models.TextChoices):
    CLICK = "click", _("Click")
    PAYME = "payme", _("Payme")
    UZUM = "uzum", _("Uzum")


class ProviderConfig(models.Model):
    """Per-tenant provider credentials. One row per provider (unique). Credential
    fields are EncryptedChar (TD-11) and write-only in the serializer."""

    provider = models.CharField(max_length=8, choices=Provider.choices)
    is_active = models.BooleanField(default=True)

    click_service_id = models.CharField(max_length=64, blank=True)
    click_merchant_id = models.CharField(max_length=64, blank=True)
    click_secret_key = EncryptedCharField(max_length=255, blank=True)

    payme_merchant_id = models.CharField(max_length=64, blank=True)
    payme_key = EncryptedCharField(max_length=255, blank=True)
    payme_test_key = EncryptedCharField(max_length=255, blank=True)

    uzum_merchant_id = models.CharField(max_length=64, blank=True)
    uzum_api_key = EncryptedCharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("provider",)
        constraints = [
            models.UniqueConstraint(fields=("provider",), name="providerconfig_one_per_provider"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.provider}{'' if self.is_active else ' (inactive)'}"


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", _("Cash")
        CLICK = "click", _("Click")
        PAYME = "payme", _("Payme")
        UZUM = "uzum", _("Uzum")
        BANK_TRANSFER = "bank_transfer", _("Bank transfer")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        PROCESSING = "processing", _("Processing")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")
        REFUNDED = "refunded", _("Refunded")

    class Allocation(models.TextChoices):
        AUTO = "auto", _("Auto")
        MANUAL_REVIEW = "manual_review", _("Manual review")
        ALLOCATED = "allocated", _("Allocated")

    provider = models.CharField(max_length=16, choices=Method.choices, db_index=True)
    amount_uzs = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, default="UZS")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    idempotency_key = models.CharField(max_length=64, unique=True)
    provider_txn_id = models.CharField(max_length=64, blank=True, db_index=True)
    provider_state = models.SmallIntegerField(null=True, blank=True)  # Payme 1/2/-1/-2
    provider_created_at_ms = models.BigIntegerField(null=True, blank=True)
    cancel_reason = models.SmallIntegerField(null=True, blank=True)
    account_ref = models.CharField(max_length=64, blank=True)  # what the payer entered (e.g. invoice number)
    allocation_status = models.CharField(max_length=16, choices=Allocation.choices, default=Allocation.AUTO)
    cashier_shift = models.ForeignKey(
        "finance.CashierShift", on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    payer = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("provider", "status")),
            models.Index(fields=("status", "created_at")),
            # The default payments log is newest-first and usually unfiltered; the
            # (status, created_at) composite can't serve the ordering without a status
            # filter. Payment is one row per transaction (high volume) — index the sort.
            models.Index(fields=("-created_at", "id"), name="payment_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.provider}:{self.amount_uzs} [{self.status}]"

    # --- Payme transaction-shape adapter (used by the Payme JSON-RPC store) ---
    @property
    def create_time_ms(self) -> int:
        return self.provider_created_at_ms or 0

    @property
    def perform_time_ms(self) -> int:
        return int(self.metadata.get("perform_time_ms", 0) or 0)

    @property
    def cancel_time_ms(self) -> int:
        return int(self.metadata.get("cancel_time_ms", 0) or 0)


class PaymentAttempt(models.Model):
    """One provider round-trip (request + response). Append-only history."""

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="attempts")
    attempt_no = models.PositiveSmallIntegerField()
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("payment", "attempt_no")
        indexes = [models.Index(fields=("payment", "attempt_no"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"attempt#{self.attempt_no} of payment {self.payment_id}"


class WebhookEvent(models.Model):
    """Replay-protection ledger (D3-B-6). ``(provider, event_id)`` is unique — a
    replayed nonce is recorded as ``duplicate`` and side effects run zero times."""

    class Status(models.TextChoices):
        RECEIVED = "received", _("Received")
        PROCESSED = "processed", _("Processed")
        REJECTED = "rejected", _("Rejected")
        DUPLICATE = "duplicate", _("Duplicate")

    provider = models.CharField(max_length=16)
    event_id = models.CharField(max_length=128)  # provider txn id / Payme id / nonce
    signature_valid = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    remote_ip = models.GenericIPAddressField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "event_id"), name="webhookevent_provider_event_unique"
            ),
        ]
        indexes = [models.Index(fields=("provider", "event_id"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.provider}:{self.event_id} [{self.status}]"


class FiscalReceipt(models.Model):
    """Soliq fiscal receipt for a completed payment (TD-7). One per payment."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        SUBMITTED = "submitted", _("Submitted")
        CONFIRMED = "confirmed", _("Confirmed")
        FAILED = "failed", _("Failed")

    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name="fiscal_receipt")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    fiscal_sign = models.CharField(max_length=128, blank=True)
    qr_url = models.URLField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    submitted_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover
        return f"receipt for payment {self.payment_id} [{self.status}]"
