from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("notifications", "0013_notification_delivery_claims")]

    operations = [
        migrations.RenameIndex(
            model_name="notificationdelivery",
            old_name="notif_delivery_status_created_idx",
            new_name="notif_delivery_status_created",
        ),
    ]
