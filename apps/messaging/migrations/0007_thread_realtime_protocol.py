from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

DELIVERABLE = ("captured", "resolved")


def backfill_message_senders(apps, schema_editor):
    """Resolve historical senders only from the exact seat in that thread."""

    Message = apps.get_model("messaging", "Message")
    Participant = apps.get_model("messaging", "ThreadParticipant")
    database = schema_editor.connection.alias
    last_pk = 0
    while True:
        rows = list(
            Message.objects.using(database)
            .filter(pk__gt=last_pk, sender_id__isnull=False)
            .order_by("pk")[:500]
        )
        if not rows:
            return
        last_pk = rows[-1].pk
        thread_ids = {row.thread_id for row in rows}
        user_ids = {row.sender_id for row in rows}
        seats: dict[tuple[int, int], list[tuple[str, int]]] = {}
        for thread_id, user_id, kind, principal_id in Participant.objects.using(database).filter(
            thread_id__in=thread_ids,
            user_id__in=user_ids,
            attribution_status__in=DELIVERABLE,
        ).values_list("thread_id", "user_id", "principal_kind", "principal_id"):
            seats.setdefault((int(thread_id), int(user_id)), []).append((str(kind), int(principal_id)))

        changed = []
        for row in rows:
            candidates = seats.get((row.thread_id, row.sender_id), [])
            if len(candidates) == 1:
                row.sender_principal_kind, row.sender_principal_id = candidates[0]
                row.sender_attribution_status = "resolved"
            elif len(candidates) > 1:
                row.sender_principal_kind = None
                row.sender_principal_id = None
                row.sender_attribution_status = "conflicting"
            else:
                row.sender_principal_kind = None
                row.sender_principal_id = None
                row.sender_attribution_status = "unresolved"
            changed.append(row)
        Message.objects.using(database).bulk_update(
            changed,
            ("sender_principal_kind", "sender_principal_id", "sender_attribution_status"),
            batch_size=500,
        )


BACKFILL_READ_CURSOR_SQL = r"""
UPDATE messaging_threadparticipant AS participant
SET last_read_message_id = (
    SELECT message.id
    FROM messaging_message AS message
    WHERE message.thread_id = participant.thread_id
      AND message.created_at <= participant.last_read_at
    ORDER BY message.created_at DESC, message.id DESC
    LIMIT 1
)
WHERE participant.last_read_at IS NOT NULL
  AND participant.last_read_message_id IS NULL;
"""


REALTIME_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION messaging_validate_message_sender_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    principal_is_live boolean := false;
    principal_has_seat boolean := false;
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.sender_id IS NOT NULL
       AND NEW.sender_id IS NULL
       AND NEW.sender_principal_kind IS NOT DISTINCT FROM OLD.sender_principal_kind
       AND NEW.sender_principal_id IS NOT DISTINCT FROM OLD.sender_principal_id
       AND NEW.sender_attribution_status IS NOT DISTINCT FROM OLD.sender_attribution_status THEN
        RETURN NEW;
    END IF;
    IF NEW.sender_attribution_status NOT IN ('captured', 'resolved') THEN
        RETURN NEW;
    END IF;

    CASE NEW.sender_principal_kind
        WHEN 'student' THEN
            SELECT EXISTS (
                SELECT 1 FROM students_studentprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = NEW.sender_principal_id
                  AND principal.user_id = NEW.sender_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO principal_is_live;
        WHEN 'teacher' THEN
            SELECT EXISTS (
                SELECT 1 FROM teachers_teacherprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = NEW.sender_principal_id
                  AND principal.user_id = NEW.sender_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO principal_is_live;
        WHEN 'parent' THEN
            SELECT EXISTS (
                SELECT 1 FROM parents_parentprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = NEW.sender_principal_id
                  AND principal.user_id = NEW.sender_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO principal_is_live;
        WHEN 'staff' THEN
            SELECT EXISTS (
                SELECT 1 FROM org_staffprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = NEW.sender_principal_id
                  AND principal.user_id = NEW.sender_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO principal_is_live;
        ELSE
            principal_is_live := false;
    END CASE;

    SELECT EXISTS (
        SELECT 1
        FROM messaging_threadparticipant participant
        WHERE participant.thread_id = NEW.thread_id
          AND participant.user_id = NEW.sender_id
          AND participant.principal_kind = NEW.sender_principal_kind
          AND participant.principal_id = NEW.sender_principal_id
          AND participant.attribution_status IN ('captured', 'resolved')
    ) INTO principal_has_seat;

    IF NOT principal_is_live OR NOT principal_has_seat THEN
        RAISE EXCEPTION 'message sender principal is not live or seated in thread'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION messaging_guard_message_sender_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.sender_principal_kind IS NOT DISTINCT FROM OLD.sender_principal_kind
       AND NEW.sender_principal_id IS NOT DISTINCT FROM OLD.sender_principal_id
       AND NEW.sender_attribution_status IS NOT DISTINCT FROM OLD.sender_attribution_status THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'message sender attribution is immutable'
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION messaging_validate_realtime_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    principal_is_live boolean := false;
    principal_has_seat boolean := false;
    message_is_in_thread boolean := false;
    event_semantics_match boolean := false;
    declared_sequence bigint;
    expected_sequence bigint;
BEGIN
    IF NEW.actor_id IS NULL THEN
        RAISE EXCEPTION 'realtime event actor is required at creation'
            USING ERRCODE = '23514';
    END IF;
    CASE NEW.actor_principal_kind
        WHEN 'student' THEN
            SELECT EXISTS (
                SELECT 1 FROM students_studentprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = NEW.actor_principal_id
                  AND principal.user_id = NEW.actor_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO principal_is_live;
        WHEN 'teacher' THEN
            SELECT EXISTS (
                SELECT 1 FROM teachers_teacherprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = NEW.actor_principal_id
                  AND principal.user_id = NEW.actor_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO principal_is_live;
        WHEN 'parent' THEN
            SELECT EXISTS (
                SELECT 1 FROM parents_parentprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = NEW.actor_principal_id
                  AND principal.user_id = NEW.actor_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO principal_is_live;
        WHEN 'staff' THEN
            SELECT EXISTS (
                SELECT 1 FROM org_staffprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = NEW.actor_principal_id
                  AND principal.user_id = NEW.actor_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO principal_is_live;
        ELSE
            principal_is_live := false;
    END CASE;
    SELECT EXISTS (
        SELECT 1 FROM messaging_threadparticipant participant
        WHERE participant.thread_id = NEW.thread_id
          AND participant.user_id = NEW.actor_id
          AND participant.principal_kind = NEW.actor_principal_kind
          AND participant.principal_id = NEW.actor_principal_id
          AND participant.attribution_status IN ('captured', 'resolved')
    ) INTO principal_has_seat;
    SELECT EXISTS (
        SELECT 1 FROM messaging_message message
        WHERE message.id = NEW.message_id AND message.thread_id = NEW.thread_id
    ) INTO message_is_in_thread;

    IF NEW.kind = 'message.created' THEN
        SELECT EXISTS (
            SELECT 1 FROM messaging_message message
            WHERE message.id = NEW.message_id
              AND message.thread_id = NEW.thread_id
              AND message.sender_id = NEW.actor_id
              AND message.sender_principal_kind = NEW.actor_principal_kind
              AND message.sender_principal_id = NEW.actor_principal_id
              AND message.sender_attribution_status IN ('captured', 'resolved')
        ) INTO event_semantics_match;
    ELSIF NEW.kind = 'read.updated' THEN
        SELECT EXISTS (
            SELECT 1 FROM messaging_threadparticipant participant
            WHERE participant.thread_id = NEW.thread_id
              AND participant.user_id = NEW.actor_id
              AND participant.principal_kind = NEW.actor_principal_kind
              AND participant.principal_id = NEW.actor_principal_id
              AND participant.attribution_status IN ('captured', 'resolved')
              AND participant.last_read_message_id = NEW.message_id
        ) INTO event_semantics_match;
    END IF;

    SELECT realtime_sequence
    INTO declared_sequence
    FROM messaging_thread
    WHERE id = NEW.thread_id;
    SELECT COALESCE(MAX(sequence), 0) + 1
    INTO expected_sequence
    FROM messaging_threadrealtimeevent
    WHERE thread_id = NEW.thread_id;

    IF NOT principal_is_live
       OR NOT principal_has_seat
       OR NOT message_is_in_thread
       OR NOT event_semantics_match
       OR NEW.sequence IS DISTINCT FROM declared_sequence
       OR NEW.sequence IS DISTINCT FROM expected_sequence THEN
        RAISE EXCEPTION 'realtime event principal or message does not belong to thread'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION messaging_validate_participant_read_cursor()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    message_is_in_thread boolean := false;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.thread_id IS DISTINCT FROM OLD.thread_id THEN
        RAISE EXCEPTION 'messaging participant thread is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.last_read_message_id IS NOT NULL
       AND (
           NEW.last_read_message_id IS NULL
           OR NEW.last_read_message_id < OLD.last_read_message_id
       ) THEN
        RAISE EXCEPTION 'messaging read cursor cannot move backwards'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.last_read_at IS NOT NULL
       AND (NEW.last_read_at IS NULL OR NEW.last_read_at < OLD.last_read_at) THEN
        RAISE EXCEPTION 'messaging read timestamp cannot move backwards'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.last_read_message_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.last_read_at IS NULL THEN
        RAISE EXCEPTION 'messaging read cursor requires a timestamp'
            USING ERRCODE = '23514';
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM messaging_message message
        WHERE message.id = NEW.last_read_message_id
          AND message.thread_id = NEW.thread_id
    ) INTO message_is_in_thread;
    IF NOT message_is_in_thread THEN
        RAISE EXCEPTION 'messaging read cursor does not belong to thread'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION messaging_guard_realtime_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'messaging realtime events are append-only'
            USING ERRCODE = '55000';
    END IF;
    -- Preserve immutable attribution when a bridge User is deliberately hard
    -- deleted: only the nullable compatibility FK may transition to NULL.
    IF OLD.actor_id IS NOT NULL
       AND NEW.actor_id IS NULL
       AND (to_jsonb(NEW) - 'actor_id') = (to_jsonb(OLD) - 'actor_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'messaging realtime events are append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER messaging_message_sender_snapshot_valid_insert
BEFORE INSERT ON messaging_message
FOR EACH ROW EXECUTE FUNCTION messaging_validate_message_sender_snapshot();
CREATE TRIGGER messaging_message_sender_snapshot_valid_update
BEFORE UPDATE OF sender_id, sender_principal_kind, sender_principal_id, sender_attribution_status
ON messaging_message
FOR EACH ROW EXECUTE FUNCTION messaging_validate_message_sender_snapshot();
CREATE TRIGGER messaging_message_sender_snapshot_immutable
BEFORE UPDATE OF sender_principal_kind, sender_principal_id, sender_attribution_status
ON messaging_message
FOR EACH ROW EXECUTE FUNCTION messaging_guard_message_sender_snapshot();
CREATE TRIGGER messaging_participant_read_cursor_valid_insert
BEFORE INSERT ON messaging_threadparticipant
FOR EACH ROW EXECUTE FUNCTION messaging_validate_participant_read_cursor();
CREATE TRIGGER messaging_participant_read_cursor_valid_update
BEFORE UPDATE OF thread_id, last_read_message_id, last_read_at
ON messaging_threadparticipant
FOR EACH ROW EXECUTE FUNCTION messaging_validate_participant_read_cursor();
CREATE TRIGGER messaging_realtime_event_valid_insert
BEFORE INSERT ON messaging_threadrealtimeevent
FOR EACH ROW EXECUTE FUNCTION messaging_validate_realtime_event();
CREATE TRIGGER messaging_realtime_event_append_only_update
BEFORE UPDATE ON messaging_threadrealtimeevent
FOR EACH ROW EXECUTE FUNCTION messaging_guard_realtime_event();
CREATE TRIGGER messaging_realtime_event_append_only_delete
BEFORE DELETE ON messaging_threadrealtimeevent
FOR EACH ROW EXECUTE FUNCTION messaging_guard_realtime_event();
"""


DROP_REALTIME_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS messaging_realtime_event_append_only_delete ON messaging_threadrealtimeevent;
DROP TRIGGER IF EXISTS messaging_realtime_event_append_only_update ON messaging_threadrealtimeevent;
DROP TRIGGER IF EXISTS messaging_realtime_event_valid_insert ON messaging_threadrealtimeevent;
DROP TRIGGER IF EXISTS messaging_participant_read_cursor_valid_update ON messaging_threadparticipant;
DROP TRIGGER IF EXISTS messaging_participant_read_cursor_valid_insert ON messaging_threadparticipant;
DROP TRIGGER IF EXISTS messaging_message_sender_snapshot_immutable ON messaging_message;
DROP TRIGGER IF EXISTS messaging_message_sender_snapshot_valid_update ON messaging_message;
DROP TRIGGER IF EXISTS messaging_message_sender_snapshot_valid_insert ON messaging_message;
DROP FUNCTION IF EXISTS messaging_guard_realtime_event();
DROP FUNCTION IF EXISTS messaging_validate_participant_read_cursor();
DROP FUNCTION IF EXISTS messaging_validate_realtime_event();
DROP FUNCTION IF EXISTS messaging_guard_message_sender_snapshot();
DROP FUNCTION IF EXISTS messaging_validate_message_sender_snapshot();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("messaging", "0006_threadparticipant_principal_attribution"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ThreadRealtimeEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("sequence", models.PositiveBigIntegerField()),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("message.created", "Message created"),
                            ("read.updated", "Read state updated"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "actor_principal_kind",
                    models.CharField(
                        choices=[
                            ("student", "Student"),
                            ("teacher", "Teacher"),
                            ("parent", "Parent"),
                            ("staff", "Staff"),
                        ],
                        editable=False,
                        max_length=16,
                    ),
                ),
                ("actor_principal_id", models.PositiveBigIntegerField(editable=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ("thread_id", "sequence")},
        ),
        migrations.AddField(
            model_name="message",
            name="sender_attribution_status",
            field=models.CharField(
                choices=[
                    ("captured", "Captured at write time"),
                    ("resolved", "Resolved by reviewed backfill"),
                    ("unresolved", "Unresolved"),
                    ("conflicting", "Conflicting evidence"),
                    ("quarantined", "Quarantined for review"),
                ],
                db_default="unresolved",
                default="unresolved",
                editable=False,
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="message",
            name="sender_principal_id",
            field=models.PositiveBigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="sender_principal_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("student", "Student"),
                    ("teacher", "Teacher"),
                    ("parent", "Parent"),
                    ("staff", "Staff"),
                ],
                editable=False,
                max_length=16,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="thread",
            name="realtime_sequence",
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="threadparticipant",
            name="last_read_message",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="messaging.message",
            ),
        ),
        migrations.RunPython(backfill_message_senders, migrations.RunPython.noop),
        migrations.RunSQL(BACKFILL_READ_CURSOR_SQL, migrations.RunSQL.noop),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(fields=["thread", "id"], name="message_thread_cursor_idx"),
        ),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(
                fields=["sender_attribution_status", "thread"],
                name="message_sender_review_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        sender_attribution_status__in=DELIVERABLE,
                        sender_principal_kind__in=("student", "teacher", "parent", "staff"),
                        sender_principal_id__isnull=False,
                    )
                    | models.Q(
                        sender_attribution_status__in=("unresolved", "conflicting", "quarantined"),
                        sender_principal_kind__isnull=True,
                        sender_principal_id__isnull=True,
                    )
                ),
                name="message_sender_attribution_shape",
            ),
        ),
        migrations.AddField(
            model_name="threadrealtimeevent",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="threadrealtimeevent",
            name="message",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="messaging.message",
            ),
        ),
        migrations.AddField(
            model_name="threadrealtimeevent",
            name="thread",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="realtime_events",
                to="messaging.thread",
            ),
        ),
        migrations.AddConstraint(
            model_name="threadrealtimeevent",
            constraint=models.UniqueConstraint(
                fields=("thread", "sequence"),
                name="one_realtime_sequence_per_thread",
            ),
        ),
        migrations.AddConstraint(
            model_name="threadrealtimeevent",
            constraint=models.CheckConstraint(
                condition=models.Q(sequence__gt=0),
                name="messaging_realtime_sequence_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="threadrealtimeevent",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        actor_principal_kind__in=["student", "teacher", "parent", "staff"]
                    )
                    & models.Q(actor_principal_id__gt=0)
                ),
                name="messaging_realtime_actor_shape",
            ),
        ),
        migrations.RunSQL(REALTIME_GUARDS_SQL, DROP_REALTIME_GUARDS_SQL),
    ]
