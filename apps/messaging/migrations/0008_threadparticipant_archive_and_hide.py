from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("messaging", "0007_thread_realtime_protocol")]

    operations = [
        migrations.AddField(
            model_name="threadparticipant",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="threadparticipant",
            name="hidden_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
