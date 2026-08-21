import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0008_installment_amount_positive"),
        ("org", "0018_staffprofile_staff_phone_unique_nonblank_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
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
            model_name="invoice",
            name="branch_at_issue",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="org.branch",
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="department_at_issue",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="org.department",
            ),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(
                fields=["branch_at_issue", "issue_date"],
                name="invoice_branch_issue_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(
                fields=["department_at_issue", "issue_date"],
                name="invoice_dept_issue_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status__in=("captured", "resolved"),
                        branch_at_issue__isnull=False,
                    )
                    | models.Q(
                        attribution_status__in=("unresolved", "conflicting", "quarantined"),
                        branch_at_issue__isnull=True,
                        department_at_issue__isnull=True,
                    )
                ),
                name="invoice_scope_attribution_valid",
            ),
        ),
    ]
