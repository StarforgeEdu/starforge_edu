from __future__ import annotations

from django.db import migrations

import core.fields

_BATCH_SIZE = 500


def _copy_field(model, *, source: str, target: str, database: str) -> None:
    # Keyset batches avoid holding a server-side read cursor open while writing
    # the same table. The migration is atomic, so PostgreSQL keeps the DDL lock
    # until commit and no concurrent insert can appear beyond the high-water mark.
    last_pk = None
    while True:
        rows_qs = model.objects.using(database).values_list("pk", source).order_by("pk")
        if last_pk is not None:
            rows_qs = rows_qs.filter(pk__gt=last_pk)
        rows = list(rows_qs[:_BATCH_SIZE])
        if not rows:
            break
        batch = [model(pk=pk, **{target: value or ""}) for pk, value in rows]
        model.objects.using(database).bulk_update(batch, [target], batch_size=_BATCH_SIZE)
        last_pk = rows[-1][0]

    # Reading the target invokes the encrypted field's authenticated decoder on
    # the forward path. Never drop the legacy column unless every value both
    # decrypts and exactly matches its source.
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
            raise RuntimeError(f"Safeguarding encryption verification failed for {model.__name__}.{source}.")
        last_pk = checked[-1][0]


def encrypt_legacy_safeguarding_text(apps, schema_editor) -> None:
    ParentProfile = apps.get_model("parents", "ParentProfile")
    Guardian = apps.get_model("parents", "Guardian")
    database = schema_editor.connection.alias
    _copy_field(ParentProfile, source="notes", target="notes_encrypted", database=database)
    _copy_field(
        Guardian,
        source="custody_notes",
        target="custody_notes_encrypted",
        database=database,
    )


def restore_legacy_safeguarding_text(apps, schema_editor) -> None:
    """Explicit, data-preserving reverse for controlled emergency rollback."""
    ParentProfile = apps.get_model("parents", "ParentProfile")
    Guardian = apps.get_model("parents", "Guardian")
    database = schema_editor.connection.alias
    _copy_field(ParentProfile, source="notes_encrypted", target="notes", database=database)
    _copy_field(
        Guardian,
        source="custody_notes_encrypted",
        target="custody_notes",
        database=database,
    )


class Migration(migrations.Migration):
    # The old plaintext columns survive until every shadow value has been
    # authenticated. PostgreSQL rolls the whole cutover back on any failure.
    atomic = True

    dependencies = [
        ("parents", "0008_parent_creation_scope"),
    ]

    operations = [
        migrations.AddField(
            model_name="parentprofile",
            name="notes_encrypted",
            field=core.fields.EncryptedTextField(blank=True, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="guardian",
            name="custody_notes_encrypted",
            field=core.fields.EncryptedTextField(blank=True, default=""),
            preserve_default=False,
        ),
        migrations.RunPython(
            encrypt_legacy_safeguarding_text,
            restore_legacy_safeguarding_text,
        ),
        migrations.RemoveField(
            model_name="parentprofile",
            name="notes",
        ),
        migrations.RemoveField(
            model_name="guardian",
            name="custody_notes",
        ),
        migrations.RenameField(
            model_name="parentprofile",
            old_name="notes_encrypted",
            new_name="notes",
        ),
        migrations.RenameField(
            model_name="guardian",
            old_name="custody_notes_encrypted",
            new_name="custody_notes",
        ),
    ]
