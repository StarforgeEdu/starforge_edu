"""Read-only admin for the Approvals + Ledger money engine.

The ledger is append-only ("every som that moves is one row — money can't
disappear"), and the approval request is a state machine (pending → approved →
disbursed) whose transitions and paired ledger write are owned by the service
layer. Hand-editing either through /admin/ would bypass those guarantees with no
audit record, so both are view-only here (mirrors apps/audit/admin.py). Money is
never corrected by editing a row — you post a reversing/correcting entry through
the approval flow.
"""

from __future__ import annotations

from django.contrib import admin

from apps.approvals.models import ApprovalRequest, LedgerEntry
from core.admin_mixins import ReadOnlyAdmin


@admin.register(LedgerEntry)
class LedgerEntryAdmin(ReadOnlyAdmin):
    list_display = (
        "created_at",
        "direction",
        "entry_type",
        "amount_uzs",
        "party_label",
        "branch",
        "payment_method",
        "created_by",
    )
    list_filter = ("direction", "entry_type")
    search_fields = ("party_label", "note", "entry_type", "source_kind")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(ReadOnlyAdmin):
    list_display = (
        "created_at",
        "kind",
        "title",
        "status",
        "amount_uzs",
        "branch",
        "requested_by",
        "decided_by",
        "disbursed_by",
    )
    list_filter = ("kind", "status")
    search_fields = ("title", "description", "kind", "decision_note")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
