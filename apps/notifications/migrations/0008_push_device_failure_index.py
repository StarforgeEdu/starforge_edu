from django.db import migrations, models
from django.db.models.fields.json import KeyTransform


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0007_alter_notification_event_type_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="notificationdelivery",
            index=models.Index(
                KeyTransform("device_id", models.F("provider_response")),
                models.F("created_at").desc(),
                condition=models.Q(channel="push"),
                include=("notification", "status"),
                name="notif_push_device_created_idx",
            ),
        ),
    ]
