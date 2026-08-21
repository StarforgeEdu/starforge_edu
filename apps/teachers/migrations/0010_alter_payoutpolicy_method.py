from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("teachers", "0009_protect_payout_policy_history"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payoutpolicy",
            name="method",
            field=models.CharField(
                choices=[
                    ("hourly", "Per taught hour"),
                    ("percent_of_collected_tuition", "% of collected tuition"),
                    ("flat_monthly", "Flat amount per calendar month"),
                ],
                max_length=32,
            ),
        ),
    ]
