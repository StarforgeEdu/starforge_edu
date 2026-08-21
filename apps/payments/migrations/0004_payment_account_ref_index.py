from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0003_payment_payment_created_idx")]

    operations = [
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(fields=["account_ref"], name="payment_account_ref_idx"),
        ),
    ]
