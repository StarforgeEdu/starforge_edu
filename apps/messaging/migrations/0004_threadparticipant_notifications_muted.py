from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("messaging", "0003_messageattachmentuploadgrant")]

    operations = [
        migrations.AddField(
            model_name="threadparticipant",
            name="notifications_muted",
            field=models.BooleanField(default=False),
        ),
    ]
