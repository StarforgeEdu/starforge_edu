from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0010_seed_extended_templates"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notificationdelivery",
            name="status",
            field=models.CharField(
                choices=[
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
    ]
