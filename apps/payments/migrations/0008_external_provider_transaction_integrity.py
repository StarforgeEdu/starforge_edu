from django.db import migrations, models
from django.db.models import Count

_EXTERNAL_PROVIDERS = ("click", "payme", "uzum")


def preflight_external_provider_transactions(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    duplicates = (
        Payment.objects.using(schema_editor.connection.alias)
        .filter(provider__in=_EXTERNAL_PROVIDERS)
        .exclude(provider_txn_id="")
        .values("provider", "provider_txn_id")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    duplicate_group_count = duplicates.count()
    if duplicate_group_count:
        # A duplicate identifier can represent two credits for one provider
        # transaction. Never guess which money row is authoritative: stop this
        # migration for explicit reconciliation, then rerun it unchanged.
        raise RuntimeError(
            "Cannot enforce external payment provider transaction uniqueness: "
            f"{duplicate_group_count} duplicate provider transaction group(s) require review."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0007_webhook_privacy_and_txn_integrity"),
    ]

    operations = [
        migrations.RunPython(
            preflight_external_provider_transactions,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(
                fields=("provider", "provider_txn_id"),
                condition=(models.Q(provider__in=_EXTERNAL_PROVIDERS) & ~models.Q(provider_txn_id="")),
                name="payment_provider_txn_unique",
            ),
        ),
    ]
