from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("finance", "0007_cashiershift_closed_by")]

    operations = [
        # Enforce the invariant for every new/changed row without making a safe
        # launch migration fail on a legacy bad row that needs operator review.
        # PostgreSQL NOT VALID still checks all future writes.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE finance_paymentplaninstallment "
                        "ADD CONSTRAINT installment_amount_positive "
                        "CHECK (amount_uzs > 0) NOT VALID"
                    ),
                    reverse_sql=(
                        "ALTER TABLE finance_paymentplaninstallment "
                        "DROP CONSTRAINT IF EXISTS installment_amount_positive"
                    ),
                )
            ],
            state_operations=[
                migrations.AddConstraint(
                    model_name="paymentplaninstallment",
                    constraint=models.CheckConstraint(
                        condition=models.Q(amount_uzs__gt=Decimal("0")),
                        name="installment_amount_positive",
                    ),
                )
            ],
        )
    ]
