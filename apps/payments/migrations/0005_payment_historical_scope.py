import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0009_invoice_historical_scope"),
        ("org", "0018_staffprofile_staff_phone_unique_nonblank_and_more"),
        ("payments", "0004_payment_account_ref_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="attribution_status",
            field=models.CharField(
                choices=[
                    ("captured", "Captured at write time"),
                    ("resolved", "Resolved by reviewed backfill"),
                    ("unresolved", "Unresolved"),
                    ("conflicting", "Conflicting evidence"),
                    ("quarantined", "Quarantined for review"),
                ],
                db_index=True,
                default="unresolved",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="branch_at_payment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="org.branch",
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="department_at_payment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="org.department",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["branch_at_payment", "paid_at"],
                name="payment_branch_paid_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["branch_at_payment", "created_at"],
                name="payment_branch_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["department_at_payment", "paid_at"],
                name="payment_dept_paid_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status__in=("captured", "resolved"),
                        branch_at_payment__isnull=False,
                    )
                    | models.Q(
                        attribution_status__in=("unresolved", "conflicting", "quarantined"),
                        branch_at_payment__isnull=True,
                        department_at_payment__isnull=True,
                    )
                ),
                name="payment_scope_attribution_valid",
            ),
        ),
    ]
