from django.db import migrations, models

import apps.org.models


OLD_DEFAULT = ["pdf", "mp4", "pptx", "docx", "mp3", "m4a", "jpg", "jpeg", "png", "webp"]
NEW_DEFAULT = ["pdf", "mp4", "pptx", "docx", "mp3", "m4a", "webm", "jpg", "jpeg", "png", "webp"]


def add_webm_to_default_centers(apps, schema_editor):
    CenterSettings = apps.get_model("org", "CenterSettings")
    CenterSettings.objects.filter(allowed_file_types=OLD_DEFAULT).update(
        allowed_file_types=NEW_DEFAULT,
    )


def remove_webm_from_default_centers(apps, schema_editor):
    CenterSettings = apps.get_model("org", "CenterSettings")
    CenterSettings.objects.filter(allowed_file_types=NEW_DEFAULT).update(
        allowed_file_types=OLD_DEFAULT,
    )


class Migration(migrations.Migration):
    dependencies = [("org", "0022_add_default_m4a_upload_type")]

    operations = [
        migrations.RunPython(
            add_webm_to_default_centers,
            remove_webm_from_default_centers,
        ),
        migrations.AlterField(
            model_name="centersettings",
            name="allowed_file_types",
            field=models.JSONField(default=apps.org.models._default_allowed_file_types),
        ),
    ]
