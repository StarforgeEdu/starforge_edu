from django.db import migrations, models


def preflight_payment_currency(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    invalid_count = Payment.objects.using(schema_editor.connection.alias).exclude(currency="UZS").count()
    if invalid_count:
        # Payment amounts and allocations in API v1 are UZS-denominated. Stop
        # before adding the constraint rather than silently relabelling money.
        raise RuntimeError(
            "Cannot enforce the payment UZS unit contract: "
            f"{invalid_count} payment row(s) have a non-UZS currency."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0008_external_provider_transaction_integrity"),
    ]

    operations = [
        migrations.RunPython(
            preflight_payment_currency,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(currency="UZS"),
                name="payment_currency_uzs",
            ),
        ),
    ]
