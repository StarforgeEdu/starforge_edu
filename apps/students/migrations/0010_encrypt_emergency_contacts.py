from __future__ import annotations

from django.db import migrations

import core.fields

_BATCH_SIZE = 500


def _copy_contacts(model, *, source: str, target: str, database: str) -> None:
    # Keyset batches avoid mixing a long-lived server cursor and updates on the
    # same table. The atomic DDL transaction prevents concurrent inserts from
    # appearing beyond the high-water mark during the cutover.
    last_pk = None
    while True:
        rows_qs = model.objects.using(database).values_list("pk", source).order_by("pk")
        if last_pk is not None:
            rows_qs = rows_qs.filter(pk__gt=last_pk)
        rows = list(rows_qs[:_BATCH_SIZE])
        if not rows:
            break
        batch = [model(pk=pk, **{target: value if value is not None else []}) for pk, value in rows]
        model.objects.using(database).bulk_update(batch, [target], batch_size=_BATCH_SIZE)
        last_pk = rows[-1][0]

    # This read authenticates and JSON-decodes the shadow column. Preserve the
    # source column until every migrated document exactly round-trips.
    last_pk = None
    while True:
        checked_qs = (
            model.objects.using(database).values_list("pk", source, target).order_by("pk")
        )
        if last_pk is not None:
            checked_qs = checked_qs.filter(pk__gt=last_pk)
        checked = list(checked_qs[:_BATCH_SIZE])
        if not checked:
            break
        if any(source_value != target_value for _, source_value, target_value in checked):
            raise RuntimeError("Emergency-contact encryption verification failed.")
        last_pk = checked[-1][0]


def encrypt_legacy_emergency_contacts(apps, schema_editor) -> None:
    StudentProfile = apps.get_model("students", "StudentProfile")
    _copy_contacts(
        StudentProfile,
        source="emergency_contacts",
        target="emergency_contacts_encrypted",
        database=schema_editor.connection.alias,
    )


def restore_legacy_emergency_contacts(apps, schema_editor) -> None:
    """Explicit, data-preserving reverse for controlled emergency rollback."""
    StudentProfile = apps.get_model("students", "StudentProfile")
    _copy_contacts(
        StudentProfile,
        source="emergency_contacts_encrypted",
        target="emergency_contacts",
        database=schema_editor.connection.alias,
    )


class Migration(migrations.Migration):
    # Keep add/backfill/verify/drop/rename in one rollback-safe PostgreSQL
    # transaction. This intentionally requires a maintenance-window lock.
    atomic = True

    dependencies = [
        ("students", "0009_studentprofile_student_phone_unique_nonblank_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="studentprofile",
            name="emergency_contacts_encrypted",
            field=core.fields.EncryptedJSONField(blank=True, default=list),
        ),
        migrations.RunPython(
            encrypt_legacy_emergency_contacts,
            restore_legacy_emergency_contacts,
        ),
        migrations.RemoveField(
            model_name="studentprofile",
            name="emergency_contacts",
        ),
        migrations.RenameField(
            model_name="studentprofile",
            old_name="emergency_contacts_encrypted",
            new_name="emergency_contacts",
        ),
    ]
