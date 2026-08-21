from django.db import migrations, models


def preflight_invoice_currency(apps, schema_editor):
    Invoice = apps.get_model("finance", "Invoice")
    invalid_count = Invoice.objects.using(schema_editor.connection.alias).exclude(currency="UZS").count()
    if invalid_count:
        # Every persisted amount in this v1 model is explicitly denominated by
        # a *_uzs column. Relabelling those values would corrupt their meaning;
        # legacy exceptions therefore require an explicit, reviewed repair.
        raise RuntimeError(
            "Cannot enforce the invoice UZS unit contract: "
            f"{invalid_count} invoice row(s) have a non-UZS currency."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0009_invoice_historical_scope"),
    ]

    operations = [
        migrations.RunPython(
            preflight_invoice_currency,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(currency="UZS"),
                name="invoice_currency_uzs",
            ),
        ),
    ]
