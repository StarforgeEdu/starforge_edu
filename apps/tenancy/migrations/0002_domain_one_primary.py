"""DB-level backstop for the one-primary-domain invariant.

services.set_primary_domain holds row locks while flipping is_primary; this
partial unique index makes Postgres reject any interleaving that would leave
two primary domains for the same Center.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="domain",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_primary", True)),
                fields=("tenant",),
                name="one_primary_domain_per_tenant",
            ),
        ),
    ]
