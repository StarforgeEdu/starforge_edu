from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0005_payment_historical_scope")]

    operations = [
        migrations.AddField(
            model_name="fiscalreceipt",
            name="pdf_key",
            field=models.CharField(blank=True, max_length=512),
        ),
        migrations.AddField(
            model_name="fiscalreceipt",
            name="provider_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
