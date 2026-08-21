from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


def backfill_expense_approvals(apps, schema_editor) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.ledger_maintenance = 'on'")
    Expense = apps.get_model("finance", "Expense")
    ApprovalRequest = apps.get_model("approvals", "ApprovalRequest")
    LedgerEntry = apps.get_model("approvals", "LedgerEntry")

    status_map = {
        "pending": "pending",
        "approved": "approved",
        "rejected": "rejected",
        "paid": "disbursed",
    }
    for expense in Expense.objects.filter(approval_request__isnull=True).iterator(chunk_size=500):
        request = ApprovalRequest.objects.create(
            kind="expense_record",
            branch_id=expense.branch_id,
            requested_by_id=expense.created_by_id,
            title=expense.description[:200],
            description=expense.description,
            amount_uzs=expense.amount_uzs,
            payload={
                "expense_id": expense.pk,
                "category": expense.category,
                "party_label": expense.description[:200],
                "backfilled": True,
            },
            status=status_map[expense.status],
            decided_by_id=expense.approved_by_id,
            decided_at=expense.approved_at,
            decision_note=expense.reject_reason,
            disbursed_by_id=expense.paid_by_id,
            disbursed_at=expense.paid_at,
            payment_method_id=expense.payment_method_id,
        )
        ApprovalRequest.objects.filter(pk=request.pk).update(
            created_at=expense.created_at,
            updated_at=expense.paid_at or expense.approved_at or expense.created_at,
        )
        if expense.status == "paid":
            ledger = LedgerEntry.objects.create(
                direction="out",
                entry_type="expense",
                amount_uzs=expense.amount_uzs,
                branch_id=expense.branch_id,
                party_label=expense.description[:200],
                payment_method_id=expense.payment_method_id,
                source_kind="approval_request",
                source_id=request.pk,
                note=expense.description[:255],
                created_by_id=expense.paid_by_id,
            )
            LedgerEntry.objects.filter(pk=ledger.pk).update(created_at=expense.paid_at or expense.created_at)
            ApprovalRequest.objects.filter(pk=request.pk).update(ledger_entry_id=ledger.pk)
        Expense.objects.filter(pk=expense.pk).update(approval_request_id=request.pk)


def reverse_backfill(apps, schema_editor) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.ledger_maintenance = 'on'")
    Expense = apps.get_model("finance", "Expense")
    ApprovalRequest = apps.get_model("approvals", "ApprovalRequest")
    LedgerEntry = apps.get_model("approvals", "LedgerEntry")

    request_ids = list(
        Expense.objects.filter(approval_request__payload__backfilled=True).values_list(
            "approval_request_id", flat=True
        )
    )
    Expense.objects.filter(approval_request_id__in=request_ids).update(approval_request_id=None)
    LedgerEntry.objects.filter(source_kind="approval_request", source_id__in=request_ids).delete()
    ApprovalRequest.objects.filter(pk__in=request_ids, payload__backfilled=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0003_ledgerentry_database_immutability"),
        ("finance", "0004_discount_single_use"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="approval_request",
            field=models.OneToOneField(
                blank=True,
                help_text="Immutable maker-checker request and ledger spine for this expense.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="expense",
                to="approvals.approvalrequest",
            ),
        ),
        migrations.RunPython(backfill_expense_approvals, reverse_backfill),
    ]
