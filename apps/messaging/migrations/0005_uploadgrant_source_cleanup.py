import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F


def mark_deployed_consumed_keys_as_durable(apps, schema_editor):
    Grant = apps.get_model("messaging", "MessageAttachmentUploadGrant")
    Grant.objects.using(schema_editor.connection.alias).filter(consumed_at__isnull=False).update(
        durable_key=F("key"),
        source_deleted_at=F("consumed_at"),
    )


class Migration(migrations.Migration):
    dependencies = [("messaging", "0004_threadparticipant_notifications_muted")]

    operations = [
        migrations.AddField(
            model_name="messageattachmentuploadgrant",
            name="source_deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="messageattachmentuploadgrant",
            name="durable_key",
            field=models.CharField(blank=True, max_length=512, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="messageattachmentuploadgrant",
            name="deletion_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="messageattachmentuploadgrant",
            name="durable_deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Message history and its private attachment validation metadata must
        # survive deletion of the sender's login bridge.
        migrations.AlterField(
            model_name="messageattachmentuploadgrant",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="users.user",
            ),
        ),
        # Legacy consumed keys are permanent message objects, not temporary
        # sources. Record them as durable and mark their source cleanup done.
        migrations.RunPython(mark_deployed_consumed_keys_as_durable, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="messageattachmentuploadgrant",
            index=models.Index(
                fields=["source_deleted_at", "expires_at"],
                name="message_upload_source_exp_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="messageattachmentuploadgrant",
            index=models.Index(
                fields=["durable_deleted_at", "deletion_requested_at"],
                name="message_upload_delete_idx",
            ),
        ),
    ]
