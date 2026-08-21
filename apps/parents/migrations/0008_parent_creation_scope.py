from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_parent_creation_scope(apps, schema_editor) -> None:
    """Keep every legacy creation boundary unresolved until reviewed.

    Guardian rows record no creation timestamp and student/cohort placement is
    mutable.  Even one currently linked student is therefore not
    contemporaneous evidence of where a parent profile was created.  Inferring
    from that relationship would move historical ownership after a student
    transfer and could expose the profile to the wrong scoped operator.

    The new columns already have fail-closed defaults.  Write them explicitly
    through the migration connection so the invariant is deterministic if this
    function is reused by a migration test or release rehearsal.
    """
    ParentProfile = apps.get_model("parents", "ParentProfile")
    ParentProfile.objects.using(schema_editor.connection.alias).update(
        branch_at_creation_id=None,
        department_at_creation_id=None,
        attribution_status="unresolved",
        created_by_id=None,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("org", "0019_centersettings_organization_timezone"),
        ("parents", "0007_parentprofile_parent_phone_unique_nonblank_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="parentprofile",
            name="attribution_status",
            field=models.CharField(
                choices=[
                    ("captured", "Captured at write time"),
                    ("resolved", "Resolved by reviewed backfill"),
                    ("unresolved", "Unresolved"),
                    ("conflicting", "Conflicting evidence"),
                    ("quarantined", "Quarantined for review"),
                ],
                db_default="unresolved",
                default="unresolved",
                editable=False,
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="parentprofile",
            name="branch_at_creation",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="org.branch",
            ),
        ),
        migrations.AddField(
            model_name="parentprofile",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_parent_profiles",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="parentprofile",
            name="department_at_creation",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="org.department",
            ),
        ),
        migrations.RunPython(backfill_parent_creation_scope, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="parentprofile",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status__in=("captured", "resolved"),
                        branch_at_creation__isnull=False,
                    )
                    | models.Q(
                        attribution_status__in=("unresolved", "conflicting", "quarantined"),
                        branch_at_creation__isnull=True,
                        department_at_creation__isnull=True,
                    )
                ),
                name="parent_creation_scope_valid",
            ),
        ),
        migrations.AddIndex(
            model_name="parentprofile",
            index=models.Index(
                fields=[
                    "attribution_status",
                    "branch_at_creation",
                    "department_at_creation",
                ],
                name="parent_creation_scope_idx",
            ),
        ),
    ]
