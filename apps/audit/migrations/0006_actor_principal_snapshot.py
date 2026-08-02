from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0005_audit_scope_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="actor_attribution_status",
            field=models.CharField(
                choices=[
                    ("exact", "Exact principal"),
                    ("system", "System or anonymous"),
                    ("unresolved", "Unresolved legacy actor"),
                ],
                db_default="unresolved",
                db_index=True,
                default="unresolved",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="actor_principal_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="actor_principal_kind",
            field=models.CharField(blank=True, db_default="", max_length=16),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(
                fields=[
                    "actor_attribution_status",
                    "actor_principal_kind",
                    "actor_principal_id",
                    "-created_at",
                    "-id",
                ],
                name="audit_actor_principal_time_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="auditlog",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        actor_attribution_status="exact",
                        actor_principal_kind__in=("user", "student", "teacher", "parent", "staff"),
                        actor_principal_id__isnull=False,
                    )
                    | models.Q(
                        actor_attribution_status="system",
                        actor__isnull=True,
                        actor_principal_kind="",
                        actor_principal_id__isnull=True,
                    )
                    | models.Q(
                        actor_attribution_status="unresolved",
                        actor_principal_kind="",
                        actor_principal_id__isnull=True,
                    )
                ),
                name="audit_actor_attribution_shape",
            ),
        ),
    ]
