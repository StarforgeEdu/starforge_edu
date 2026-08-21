import django.db.models.deletion
from django.db import migrations, models


def backfill_transfer_subjects(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.org_history_maintenance = 'on'")
    BranchTransfer = apps.get_model("org", "BranchTransfer")
    for transfer in BranchTransfer.objects.exclude(student_id__isnull=True).iterator():
        transfer.subject_kind = "student"
        transfer.subject_id = transfer.student_id
        transfer.subject_name = transfer.student_name
        transfer.subject_reference = transfer.student_public_id
        transfer.save(
            update_fields=(
                "subject_kind",
                "subject_id",
                "subject_name",
                "subject_reference",
            )
        )


def restore_legacy_subjects(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.org_history_maintenance = 'on'")
    BranchTransfer = apps.get_model("org", "BranchTransfer")
    BranchTransfer.objects.update(
        subject_kind="legacy",
        subject_id=None,
        subject_name="",
        subject_reference="",
    )


class Migration(migrations.Migration):
    dependencies = [("org", "0023_add_default_webm_upload_type")]

    operations = [
        migrations.AlterField(
            model_name="branchtransfer",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="branch_transfers",
                to="users.user",
            ),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="subject_id",
            field=models.PositiveBigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="subject_kind",
            field=models.CharField(
                choices=[
                    ("legacy", "Legacy record"),
                    ("student", "Student"),
                    ("teacher", "Teacher"),
                    ("staff", "Staff member"),
                    ("cohort", "Group"),
                ],
                db_index=True,
                default="legacy",
                editable=False,
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="subject_name",
            field=models.CharField(blank=True, editable=False, max_length=452),
        ),
        migrations.AddField(
            model_name="branchtransfer",
            name="subject_reference",
            field=models.CharField(blank=True, editable=False, max_length=150),
        ),
        migrations.RunPython(backfill_transfer_subjects, restore_legacy_subjects),
        migrations.AddConstraint(
            model_name="branchtransfer",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        subject_kind="legacy",
                        subject_id__isnull=True,
                        subject_name="",
                        subject_reference="",
                    )
                    | models.Q(
                        subject_kind="student",
                        subject_id=models.F("student_id"),
                        student__isnull=False,
                        user__isnull=False,
                    )
                    | (
                        models.Q(
                            subject_kind__in=("teacher", "staff"),
                            subject_id__isnull=False,
                            student__isnull=True,
                            user__isnull=False,
                        )
                        & ~models.Q(subject_name="")
                    )
                    | (
                        models.Q(
                            subject_kind="cohort",
                            subject_id__isnull=False,
                            student__isnull=True,
                            user__isnull=True,
                        )
                        & ~models.Q(subject_name="")
                    )
                ),
                name="branch_transfer_subject_consistent",
            ),
        ),
    ]
