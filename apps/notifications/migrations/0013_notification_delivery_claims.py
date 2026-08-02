from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("notifications", "0012_recipient_principal_attribution")]

    operations = [
        migrations.AddField(
            model_name="notificationdelivery",
            name="delivery_key",
            field=models.CharField(blank=True, editable=False, max_length=160, null=True),
        ),
        migrations.AlterField(
            model_name="notificationdelivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("claimed", "Claimed for provider delivery"),
                    ("unknown", "Provider outcome unknown"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("skipped_pref", "Skipped (preference off)"),
                    ("skipped_disabled", "Skipped (channel disabled by operator)"),
                    ("skipped_quiet_hours", "Skipped (quiet hours)"),
                    ("dead_token", "Dead push token"),
                ],
                db_index=True,
                max_length=24,
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationdelivery",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("delivery_key__isnull", False),
                    ("status__in", ("claimed", "unknown", "sent")),
                ),
                fields=("notification", "channel", "delivery_key"),
                name="notif_one_provider_contact_per_destination",
            ),
        ),
    ]
