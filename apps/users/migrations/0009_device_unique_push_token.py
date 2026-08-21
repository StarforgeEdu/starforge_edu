from django.db import migrations, models
from django.db.models import Count
from django.db.models.functions import SHA256


def deduplicate_push_token_fingerprints(apps, schema_editor):
    Device = apps.get_model("users", "Device")
    for device in Device.objects.exclude(push_token="").only("id", "push_token"):
        normalized = device.push_token.strip()
        if len(normalized.encode("utf-8")) > 8 * 1024:
            normalized = ""
        if normalized != device.push_token:
            Device.objects.filter(pk=device.pk).update(push_token=normalized)
    duplicates = (
        Device.objects.exclude(push_token_fingerprint="")
        .values("push_token_fingerprint")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    for duplicate in duplicates.iterator():
        fingerprint = duplicate["push_token_fingerprint"]
        keep_id = (
            Device.objects.filter(push_token_fingerprint=fingerprint)
            .order_by("-last_seen_at", "-id")
            .values_list("id", flat=True)
            .first()
        )
        Device.objects.filter(push_token_fingerprint=fingerprint).exclude(pk=keep_id).update(push_token="")


class Migration(migrations.Migration):
    dependencies = [("users", "0008_rolemembership_admin_labels")]

    operations = [
        # pgcrypto is a database-global prerequisite shared by every tenant
        # schema. Never drop it when one tenant migration is reversed.
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS pgcrypto;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddField(
            model_name="device",
            name="push_token_fingerprint",
            field=models.GeneratedField(
                db_persist=True,
                expression=models.Case(
                    models.When(push_token="", then=models.Value("")),
                    default=SHA256("push_token"),
                    output_field=models.CharField(max_length=64),
                ),
                output_field=models.CharField(max_length=64),
            ),
        ),
        # The migration is atomic, so the lock remains held through dedupe and
        # unique-index creation. Old app containers can keep omitting the new
        # generated column; PostgreSQL computes it for every old and new write.
        migrations.RunSQL(
            sql="LOCK TABLE users_device IN SHARE ROW EXCLUSIVE MODE;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunPython(
            deduplicate_push_token_fingerprints,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="device",
            constraint=models.UniqueConstraint(
                fields=("push_token_fingerprint",),
                condition=~models.Q(push_token_fingerprint=""),
                name="device_unique_push_fingerprint",
            ),
        ),
    ]
