"""Persist the authoritative page count for a consumed print upload."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("printing", "0006_staff_print_submission"),
    ]

    operations = [
        migrations.AddField(
            model_name="printuploadgrant",
            name="page_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
