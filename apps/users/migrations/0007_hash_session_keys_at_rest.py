"""Prepare Session storage for one-way bearer-token digests.

This migration deliberately does *not* rewrite live values. Production applies
migrations before replacing old application containers; hashing here would make
every existing session unreadable to those still-running nodes. New code dual-reads
legacy values and hashes them lazily, while the post-readiness ``hash_session_keys``
command performs the bulk security cutover after all old nodes have stopped.

The physical column name remains ``key`` so the schema itself is compatible with
the pre-cutover application during deployment. The ORM field is renamed to
``key_hash`` so new application code cannot mistake a stored digest for a credential.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0006_rolemembership_account_type"),
    ]

    operations = [
        migrations.RenameField(
            model_name="session",
            old_name="key",
            new_name="key_hash",
        ),
        migrations.AlterField(
            model_name="session",
            name="key_hash",
            field=models.CharField(db_column="key", db_index=True, max_length=71, unique=True),
        ),
    ]
