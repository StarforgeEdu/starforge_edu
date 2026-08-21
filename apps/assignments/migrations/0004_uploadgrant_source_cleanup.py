import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F


def mark_deployed_consumed_keys_as_durable(apps, schema_editor):
    Grant = apps.get_model("assignments", "AssignmentUploadGrant")
    Grant.objects.using(schema_editor.connection.alias).filter(consumed_at__isnull=False).update(
        durable_key=F("key"),
        source_deleted_at=F("consumed_at"),
    )


class Migration(migrations.Migration):
    dependencies = [("assignments", "0003_assignmentuploadgrant")]

    operations = [
        migrations.AddField(
            model_name="assignmentuploadgrant",
            name="source_deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="assignmentuploadgrant",
            name="durable_key",
            field=models.CharField(blank=True, max_length=512, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="assignmentuploadgrant",
            name="deletion_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="assignmentuploadgrant",
            name="durable_deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Durable assignment objects must outlive a hard-deleted uploader.
        # The server-issued key and grant retain immutable record identity;
        # deleting the user must not cascade away the validation record.
        migrations.AlterField(
            model_name="assignmentuploadgrant",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="users.user",
            ),
        ),
        # Before this release the upload key was also the durable attachment
        # key. Record it as durable and never let the staging sweep delete it.
        migrations.RunPython(mark_deployed_consumed_keys_as_durable, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="assignmentuploadgrant",
            index=models.Index(
                fields=["source_deleted_at", "expires_at"],
                name="assign_upload_source_exp_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="assignmentuploadgrant",
            index=models.Index(
                fields=["durable_deleted_at", "deletion_requested_at"],
                name="assign_upload_delete_idx",
            ),
        ),
    ]
