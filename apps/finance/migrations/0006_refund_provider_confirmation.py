import django.db.models.deletion
from django.db import migrations, models


def backfill_completed_refund_ledger(apps, schema_editor) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.ledger_maintenance = 'on'")
    Refund = apps.get_model("finance", "Refund")
    LedgerEntry = apps.get_model("approvals", "LedgerEntry")

    for refund in Refund.objects.filter(state="completed", ledger_entry__isnull=True).iterator(
        chunk_size=500
    ):
        invoice = refund.invoice
        entry = LedgerEntry.objects.create(
            direction="out",
            entry_type="refund",
            amount_uzs=refund.amount_uzs,
            branch_id=invoice.student.branch_id,
            party_label=str(invoice.student)[:200],
            source_kind="refund",
            source_id=refund.pk,
            note=refund.reason[:255],
            created_by_id=refund.requested_by_id,
        )
        LedgerEntry.objects.filter(pk=entry.pk).update(created_at=refund.updated_at)
        # These rows predate provider-confirmation tracking. Preserve them as
        # completed, but label the synthesized reference explicitly as legacy so
        # operators never mistake it for a live provider acknowledgement.
        Refund.objects.filter(pk=refund.pk).update(
            ledger_entry_id=entry.pk,
            provider="legacy",
            provider_refund_id=f"legacy:{refund.pk}",
            provider_confirmed_at=refund.updated_at,
        )


def reverse_refund_ledger(apps, schema_editor) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.ledger_maintenance = 'on'")
    Refund = apps.get_model("finance", "Refund")
    LedgerEntry = apps.get_model("approvals", "LedgerEntry")

    ids = list(
        Refund.objects.filter(ledger_entry__source_kind="refund").values_list("ledger_entry_id", flat=True)
    )
    Refund.objects.filter(ledger_entry_id__in=ids).update(ledger_entry_id=None)
    LedgerEntry.objects.filter(pk__in=ids, source_kind="refund").delete()


class Migration(migrations.Migration):
    dependencies = [("finance", "0005_expense_approval_spine")]

    operations = [
        migrations.AddField(
            model_name="refund",
            name="ledger_entry",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="refund",
                to="approvals.ledgerentry",
            ),
        ),
        migrations.AddField(
            model_name="refund",
            name="provider",
            field=models.CharField(blank=True, db_index=True, max_length=16),
        ),
        migrations.AddField(
            model_name="refund",
            name="provider_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="refund",
            name="provider_refund_id",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.UniqueConstraint(
                condition=~models.Q(provider_refund_id=""),
                fields=("provider", "provider_refund_id"),
                name="refund_provider_reference_unique",
            ),
        ),
        migrations.RunPython(backfill_completed_refund_ledger, reverse_refund_ledger),
    ]
