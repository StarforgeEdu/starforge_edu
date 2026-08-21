from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0003_ledgerentry_database_immutability"),
    ]

    operations = [
        migrations.AddField(
            model_name="approvalrequest",
            name="domain_dedupe_key",
            field=models.CharField(blank=True, editable=False, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="approvalrequest",
            name="idempotency_key_hash",
            field=models.CharField(blank=True, editable=False, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="approvalrequest",
            name="operation_fingerprint",
            field=models.CharField(
                blank=True,
                db_default="",
                editable=False,
                max_length=64,
            ),
        ),
        migrations.AddConstraint(
            model_name="approvalrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("idempotency_key_hash__isnull", False)),
                fields=("idempotency_key_hash",),
                name="approval_idempotency_key_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="approvalrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("domain_dedupe_key__isnull", False)),
                fields=("domain_dedupe_key",),
                name="approval_domain_dedupe_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="approvalrequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        ("domain_dedupe_key__isnull", True),
                        ("idempotency_key_hash__isnull", True),
                    )
                    | ~models.Q(("operation_fingerprint", ""))
                ),
                name="approval_key_has_fingerprint",
            ),
        ),
    ]
