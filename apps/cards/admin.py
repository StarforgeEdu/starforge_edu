from django.contrib import admin

from apps.cards.models import Card, CardScan, CardType, Wallet, WalletTransaction
from core.admin_mixins import ReadOnlyAdmin


@admin.register(CardType)
class CardTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_at", "created_by")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(Card)
class CardAdmin(ReadOnlyAdmin):
    list_display = ("id", "student", "card_type", "is_active", "issued_at", "issued_by")
    list_filter = ("is_active", "card_type")
    search_fields = ("code", "student__first_name", "student__last_name")
    date_hierarchy = "issued_at"


@admin.register(CardScan)
class CardScanAdmin(ReadOnlyAdmin):
    list_display = ("scanned_at", "card", "was_valid", "scanned_by", "note")
    list_filter = ("was_valid", "card__card_type")
    search_fields = ("card__code", "card__student__first_name", "card__student__last_name", "note")
    date_hierarchy = "scanned_at"
    ordering = ("-scanned_at",)


@admin.register(Wallet)
class WalletAdmin(ReadOnlyAdmin):
    list_display = ("student", "balance_uzs", "updated_at")
    search_fields = ("student__first_name", "student__last_name")


@admin.register(WalletTransaction)
class WalletTransactionAdmin(ReadOnlyAdmin):
    list_display = (
        "created_at",
        "wallet",
        "kind",
        "amount_uzs",
        "balance_after_uzs",
        "created_by",
    )
    list_filter = ("kind",)
    search_fields = ("wallet__student__first_name", "wallet__student__last_name", "note")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
