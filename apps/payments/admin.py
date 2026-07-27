from __future__ import annotations

from django.contrib import admin

from apps.payments.models import FiscalReceipt, Payment, PaymentAttempt, ProviderConfig, WebhookEvent
from core.admin_mixins import ReadOnlyAdmin


@admin.register(ProviderConfig)
class ProviderConfigAdmin(admin.ModelAdmin):
    list_display = ("provider", "is_active", "updated_at")
    list_filter = ("provider", "is_active")
    # Credentials are encrypted (TD-11) — never list/search them in admin.
    exclude = ("click_secret_key", "payme_key", "payme_test_key", "uzum_api_key")


class PaymentAttemptInline(admin.TabularInline):
    """Provider round-trips for this payment — append-only history written by the
    integration layer, so view-only here (mirrors the audit/ledger pattern)."""

    model = PaymentAttempt
    extra = 0
    fields = ("attempt_no", "error_code", "request_payload", "response_payload", "created_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None) -> bool:
        return False


class FiscalReceiptInline(admin.TabularInline):
    """The Soliq fiscal receipt for this payment (one per payment)."""

    model = FiscalReceipt
    extra = 0
    max_num = 1
    fields = ("status", "fiscal_sign", "attempts", "submitted_at", "confirmed_at")
    show_change_link = True


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "provider",
        "amount_uzs",
        "status",
        "allocation_status",
        "payer",
        "provider_txn_id",
        "paid_at",
    )
    list_filter = ("provider", "status", "allocation_status")
    search_fields = ("provider_txn_id", "account_ref", "idempotency_key")
    readonly_fields = ("idempotency_key", "provider_txn_id", "metadata", "created_at", "updated_at")
    autocomplete_fields = ("payer",)
    # finance.CashierShift admin declares no search_fields — keep it raw_id (admin.E040).
    raw_id_fields = ("cashier_shift",)
    list_select_related = ("payer",)
    inlines = (PaymentAttemptInline, FiscalReceiptInline)


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(ReadOnlyAdmin):
    """Provider round-trip log — written by the integration layer, so view-only
    here (append-only history)."""

    list_display = ("id", "payment", "attempt_no", "error_code", "created_at")
    list_filter = ("error_code",)
    search_fields = ("payment__idempotency_key", "payment__provider_txn_id", "error_code")
    autocomplete_fields = ("payment",)
    list_select_related = ("payment",)
    date_hierarchy = "created_at"


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    """Replay-protection intake ledger — written by the webhook receiver, so
    view-only here (append-only)."""

    list_display = ("id", "provider", "event_id", "status", "signature_valid", "created_at")
    list_filter = ("provider", "status", "signature_valid")
    search_fields = ("event_id",)
    readonly_fields = ("provider", "event_id", "payload", "remote_ip", "created_at", "processed_at")
    date_hierarchy = "created_at"


@admin.register(FiscalReceipt)
class FiscalReceiptAdmin(admin.ModelAdmin):
    list_display = ("id", "payment", "status", "fiscal_sign", "attempts", "confirmed_at")
    list_filter = ("status",)
    search_fields = ("fiscal_sign", "payment__idempotency_key", "payment__provider_txn_id")
    autocomplete_fields = ("payment",)
    list_select_related = ("payment",)
