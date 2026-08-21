from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cards", "0002_wallet_wallettransaction_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="wallettransaction",
            name="actor_principal_kind",
            field=models.CharField(blank=True, editable=False, max_length=16),
        ),
        migrations.AddField(
            model_name="wallettransaction",
            name="actor_principal_id",
            field=models.PositiveBigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="wallettransaction",
            name="idempotency_key_hash",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="wallettransaction",
            name="operation_fingerprint",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="wallettransaction",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        actor_principal_kind="",
                        actor_principal_id__isnull=True,
                        idempotency_key_hash__isnull=True,
                        operation_fingerprint="",
                    )
                    | (
                        ~models.Q(actor_principal_kind="")
                        & models.Q(actor_principal_id__isnull=False)
                        & models.Q(idempotency_key_hash__isnull=False)
                        & ~models.Q(operation_fingerprint="")
                    )
                ),
                name="wallet_txn_idempotency_shape",
            ),
        ),
    ]
