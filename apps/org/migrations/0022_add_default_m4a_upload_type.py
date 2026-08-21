"""Add the reviewed mobile voice-note container to unchanged default policies."""

from django.db import migrations, models

import apps.org.models

OLD_DEFAULT = ["pdf", "mp4", "pptx", "docx", "mp3", "jpg", "jpeg", "png", "webp"]
NEW_DEFAULT = ["pdf", "mp4", "pptx", "docx", "mp3", "m4a", "jpg", "jpeg", "png", "webp"]


def add_m4a_to_unchanged_defaults(apps, schema_editor):
    """Preserve operator restrictions; only advance the exact legacy default."""

    CenterSettings = apps.get_model("org", "CenterSettings")
    CenterSettings.objects.using(schema_editor.connection.alias).filter(
        allowed_file_types=OLD_DEFAULT
    ).update(allowed_file_types=NEW_DEFAULT)


def remove_m4a_from_unchanged_defaults(apps, schema_editor):
    CenterSettings = apps.get_model("org", "CenterSettings")
    CenterSettings.objects.using(schema_editor.connection.alias).filter(
        allowed_file_types=NEW_DEFAULT
    ).update(allowed_file_types=OLD_DEFAULT)


class Migration(migrations.Migration):
    dependencies = [
        ("org", "0021_durable_center_settings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="centersettings",
            name="allowed_file_types",
            field=models.JSONField(default=apps.org.models._default_allowed_file_types),
        ),
        migrations.RunPython(
            add_m4a_to_unchanged_defaults,
            remove_m4a_from_unchanged_defaults,
        ),
    ]
