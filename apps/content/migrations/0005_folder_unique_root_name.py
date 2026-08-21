from django.db import migrations, models
from django.db.models import Count


def rename_duplicate_root_folders(apps, schema_editor):
    Folder = apps.get_model("content", "Folder")
    duplicate_keys = (
        Folder.objects.filter(parent_id__isnull=True)
        .values("library_id", "name")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )

    for key in duplicate_keys.iterator():
        folders = Folder.objects.filter(
            library_id=key["library_id"],
            parent_id__isnull=True,
            name=key["name"],
        ).order_by("pk")
        for folder in folders[1:]:
            counter = 0
            while True:
                discriminator = str(folder.pk) if counter == 0 else f"{folder.pk}-{counter}"
                suffix = f" (duplicate {discriminator})"
                candidate = f"{key['name'][: 200 - len(suffix)]}{suffix}"
                collision = Folder.objects.filter(
                    library_id=key["library_id"],
                    parent_id__isnull=True,
                    name=candidate,
                ).exclude(pk=folder.pk)
                if not collision.exists():
                    break
                counter += 1
            Folder.objects.filter(pk=folder.pk).update(name=candidate)


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0004_librarymaterial"),
    ]

    operations = [
        migrations.RunPython(rename_duplicate_root_folders, reverse_code=migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="folder",
            constraint=models.UniqueConstraint(
                fields=("library", "name"),
                condition=models.Q(parent__isnull=True),
                name="folder_unique_root_name",
            ),
        ),
    ]
