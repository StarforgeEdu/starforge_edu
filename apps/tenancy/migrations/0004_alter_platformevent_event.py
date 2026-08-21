from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenancy", "0003_platformevent")]

    operations = [
        migrations.AlterField(
            model_name="platformevent",
            name="event",
            field=models.CharField(
                choices=[
                    ("center.suspended", "Center suspended"),
                    ("center.activated", "Center activated"),
                    ("center.trial_extended", "Center trial extended"),
                    ("center.trial_expired", "Center trial expired"),
                    ("center.created", "Center created"),
                    ("subscription.changed", "Subscription changed"),
                    ("impersonation.minted", "Impersonation token minted"),
                ],
                db_index=True,
                max_length=64,
            ),
        )
    ]
