from importlib import import_module

from django.db import migrations

MUTATION_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION messaging_role_principal_is_live(
    p_kind text,
    p_principal_id bigint,
    p_user_id bigint
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    result boolean := false;
BEGIN
    IF p_user_id IS NULL OR p_principal_id IS NULL THEN
        RETURN false;
    END IF;
    CASE p_kind
        WHEN 'student' THEN
            SELECT EXISTS (
                SELECT 1 FROM students_studentprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = p_principal_id
                  AND principal.user_id = p_user_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO result;
        WHEN 'teacher' THEN
            SELECT EXISTS (
                SELECT 1 FROM teachers_teacherprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = p_principal_id
                  AND principal.user_id = p_user_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO result;
        WHEN 'parent' THEN
            SELECT EXISTS (
                SELECT 1 FROM parents_parentprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = p_principal_id
                  AND principal.user_id = p_user_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO result;
        WHEN 'staff' THEN
            SELECT EXISTS (
                SELECT 1 FROM org_staffprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                WHERE principal.id = p_principal_id
                  AND principal.user_id = p_user_id
                  AND principal.is_active AND bridge_user.is_active
            ) INTO result;
        ELSE
            result := false;
    END CASE;
    RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION messaging_validate_message_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    sender_user_id bigint;
    sender_kind text;
    current_sender_principal_id bigint;
    sender_status text;
    message_thread_id bigint;
    current_version bigint;
    current_body text;
    current_edited_at timestamptz;
    current_deleted_at timestamptz;
BEGIN
    IF NEW.actor_id IS NULL THEN
        RAISE EXCEPTION 'message revision actor is required at creation'
            USING ERRCODE = '23514';
    END IF;
    SELECT message.sender_id, message.sender_principal_kind, message.sender_principal_id,
           message.sender_attribution_status, message.thread_id, message.version,
           message.body, message.edited_at, message.deleted_at
    INTO sender_user_id, sender_kind, current_sender_principal_id,
         sender_status, message_thread_id, current_version, current_body,
         current_edited_at, current_deleted_at
    FROM messaging_message message
    WHERE message.id = NEW.message_id;
    IF NOT FOUND
       OR sender_status NOT IN ('captured', 'resolved')
       OR NEW.actor_id IS DISTINCT FROM sender_user_id
       OR NEW.actor_principal_kind IS DISTINCT FROM sender_kind
       OR NEW.actor_principal_id IS DISTINCT FROM current_sender_principal_id
       OR NOT messaging_role_principal_is_live(
           NEW.actor_principal_kind,
           NEW.actor_principal_id,
           NEW.actor_id
       )
       OR NOT EXISTS (
           SELECT 1 FROM messaging_threadparticipant participant
           WHERE participant.thread_id = message_thread_id
             AND participant.user_id = NEW.actor_id
             AND participant.principal_kind = NEW.actor_principal_kind
             AND participant.principal_id = NEW.actor_principal_id
             AND participant.attribution_status IN ('captured', 'resolved')
       )
       OR NEW.version IS DISTINCT FROM current_version + 1
       OR NEW.kind NOT IN ('edited', 'deleted')
       OR NEW.previous_body IS DISTINCT FROM current_body
       OR NEW.previous_edited_at IS DISTINCT FROM current_edited_at
       OR NEW.previous_deleted_at IS DISTINCT FROM current_deleted_at THEN
        RAISE EXCEPTION 'message revision does not match the exact author or prior state'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION messaging_guard_message_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.actor_id IS NOT NULL
       AND NEW.actor_id IS NULL
       AND (to_jsonb(NEW) - 'actor_id') = (to_jsonb(OLD) - 'actor_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'message revisions are append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION messaging_validate_message_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    revision_kind text;
BEGIN
    IF NEW.version IS DISTINCT FROM OLD.version + 1 THEN
        RAISE EXCEPTION 'message mutation must advance exactly one version'
            USING ERRCODE = '23514';
    END IF;
    SELECT kind INTO revision_kind
    FROM messaging_messagerevision revision
    WHERE revision.message_id = OLD.id
      AND revision.version = NEW.version
      AND revision.previous_body IS NOT DISTINCT FROM OLD.body
      AND revision.previous_edited_at IS NOT DISTINCT FROM OLD.edited_at
      AND revision.previous_deleted_at IS NOT DISTINCT FROM OLD.deleted_at;

    IF revision_kind = 'edited'
       AND OLD.deleted_at IS NULL
       AND NEW.deleted_at IS NOT DISTINCT FROM OLD.deleted_at
       AND NEW.body IS DISTINCT FROM OLD.body
       AND NEW.edited_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF revision_kind = 'deleted'
       AND OLD.deleted_at IS NULL
       AND NEW.deleted_at IS NOT NULL
       AND NEW.body IS NOT DISTINCT FROM OLD.body
       AND NEW.edited_at IS NOT DISTINCT FROM OLD.edited_at THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'message mutation requires a matching immutable revision'
        USING ERRCODE = '23514';
END;
$$;

CREATE OR REPLACE FUNCTION messaging_validate_message_reaction()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    message_thread_id bigint;
    message_deleted_at timestamptz;
BEGIN
    IF NEW.reactor_id IS NULL THEN
        RAISE EXCEPTION 'message reaction actor is required at creation'
            USING ERRCODE = '23514';
    END IF;
    SELECT thread_id, deleted_at
    INTO message_thread_id, message_deleted_at
    FROM messaging_message
    WHERE id = NEW.message_id;
    IF NOT FOUND
       OR message_deleted_at IS NOT NULL
       OR NOT messaging_role_principal_is_live(
           NEW.reactor_principal_kind,
           NEW.reactor_principal_id,
           NEW.reactor_id
       )
       OR NOT EXISTS (
           SELECT 1 FROM messaging_threadparticipant participant
           WHERE participant.thread_id = message_thread_id
             AND participant.user_id = NEW.reactor_id
             AND participant.principal_kind = NEW.reactor_principal_kind
             AND participant.principal_id = NEW.reactor_principal_id
             AND participant.attribution_status IN ('captured', 'resolved')
       ) THEN
        RAISE EXCEPTION 'message reaction actor is not live or seated in the thread'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION messaging_guard_message_reaction()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.reactor_id IS NOT NULL
       AND NEW.reactor_id IS NULL
       AND (to_jsonb(NEW) - 'reactor_id') = (to_jsonb(OLD) - 'reactor_id') THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.removed_at IS NULL
       AND NEW.removed_at IS NOT NULL
       AND (to_jsonb(NEW) - 'removed_at') = (to_jsonb(OLD) - 'removed_at') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'message reactions are immutable except for one soft removal'
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
    principal_is_live := messaging_role_principal_is_live(
        NEW.actor_principal_kind,
        NEW.actor_principal_id,
        NEW.actor_id
    );
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
    ELSIF NEW.kind = 'message.updated' THEN
        SELECT EXISTS (
            SELECT 1 FROM messaging_message message
            WHERE message.id = NEW.message_id
              AND message.thread_id = NEW.thread_id
              AND message.sender_id = NEW.actor_id
              AND message.sender_principal_kind = NEW.actor_principal_kind
              AND message.sender_principal_id = NEW.actor_principal_id
              AND message.sender_attribution_status IN ('captured', 'resolved')
              AND message.edited_at IS NOT NULL
              AND message.deleted_at IS NULL
        ) INTO event_semantics_match;
    ELSIF NEW.kind = 'message.deleted' THEN
        SELECT EXISTS (
            SELECT 1 FROM messaging_message message
            WHERE message.id = NEW.message_id
              AND message.thread_id = NEW.thread_id
              AND message.sender_id = NEW.actor_id
              AND message.sender_principal_kind = NEW.actor_principal_kind
              AND message.sender_principal_id = NEW.actor_principal_id
              AND message.sender_attribution_status IN ('captured', 'resolved')
              AND message.deleted_at IS NOT NULL
        ) INTO event_semantics_match;
    ELSIF NEW.kind = 'reaction.added' THEN
        SELECT EXISTS (
            SELECT 1 FROM messaging_messagereaction reaction
            WHERE reaction.message_id = NEW.message_id
              AND reaction.reactor_id = NEW.actor_id
              AND reaction.reactor_principal_kind = NEW.actor_principal_kind
              AND reaction.reactor_principal_id = NEW.actor_principal_id
              AND reaction.removed_at IS NULL
        ) INTO event_semantics_match;
    ELSIF NEW.kind = 'reaction.removed' THEN
        SELECT EXISTS (
            SELECT 1 FROM messaging_messagereaction reaction
            WHERE reaction.message_id = NEW.message_id
              AND reaction.reactor_principal_kind = NEW.actor_principal_kind
              AND reaction.reactor_principal_id = NEW.actor_principal_id
              AND reaction.removed_at IS NOT NULL
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

    SELECT realtime_sequence INTO declared_sequence
    FROM messaging_thread WHERE id = NEW.thread_id;
    SELECT COALESCE(MAX(sequence), 0) + 1 INTO expected_sequence
    FROM messaging_threadrealtimeevent WHERE thread_id = NEW.thread_id;

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

CREATE TRIGGER messaging_message_revision_valid_insert
BEFORE INSERT ON messaging_messagerevision
FOR EACH ROW EXECUTE FUNCTION messaging_validate_message_revision();
CREATE TRIGGER messaging_message_revision_append_only_update
BEFORE UPDATE ON messaging_messagerevision
FOR EACH ROW EXECUTE FUNCTION messaging_guard_message_revision();
CREATE TRIGGER messaging_message_revision_append_only_delete
BEFORE DELETE ON messaging_messagerevision
FOR EACH ROW EXECUTE FUNCTION messaging_guard_message_revision();
CREATE TRIGGER messaging_message_mutation_requires_revision
BEFORE UPDATE OF body, version, edited_at, deleted_at ON messaging_message
FOR EACH ROW EXECUTE FUNCTION messaging_validate_message_mutation();
CREATE TRIGGER messaging_message_reaction_valid_insert
BEFORE INSERT ON messaging_messagereaction
FOR EACH ROW EXECUTE FUNCTION messaging_validate_message_reaction();
CREATE TRIGGER messaging_message_reaction_guard_update
BEFORE UPDATE ON messaging_messagereaction
FOR EACH ROW EXECUTE FUNCTION messaging_guard_message_reaction();
CREATE TRIGGER messaging_message_reaction_guard_delete
BEFORE DELETE ON messaging_messagereaction
FOR EACH ROW EXECUTE FUNCTION messaging_guard_message_reaction();
"""


DROP_MUTATION_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS messaging_message_reaction_guard_delete ON messaging_messagereaction;
DROP TRIGGER IF EXISTS messaging_message_reaction_guard_update ON messaging_messagereaction;
DROP TRIGGER IF EXISTS messaging_message_reaction_valid_insert ON messaging_messagereaction;
DROP TRIGGER IF EXISTS messaging_message_mutation_requires_revision ON messaging_message;
DROP TRIGGER IF EXISTS messaging_message_revision_append_only_delete ON messaging_messagerevision;
DROP TRIGGER IF EXISTS messaging_message_revision_append_only_update ON messaging_messagerevision;
DROP TRIGGER IF EXISTS messaging_message_revision_valid_insert ON messaging_messagerevision;
DROP FUNCTION IF EXISTS messaging_guard_message_reaction();
DROP FUNCTION IF EXISTS messaging_validate_message_reaction();
DROP FUNCTION IF EXISTS messaging_validate_message_mutation();
DROP FUNCTION IF EXISTS messaging_guard_message_revision();
DROP FUNCTION IF EXISTS messaging_validate_message_revision();
"""

# Reversing 0009 removes columns/tables referenced by the expanded validator.
# Restore the exact immutable validator shipped by 0007 before that schema
# reversal. Keeping this derived from the immutable older migration avoids two
# independently maintained copies of a security-sensitive PostgreSQL function.
_OLD_GUARDS_SQL = import_module("apps.messaging.migrations.0007_thread_realtime_protocol").REALTIME_GUARDS_SQL
_OLD_VALIDATOR_START = _OLD_GUARDS_SQL.index("CREATE OR REPLACE FUNCTION messaging_validate_realtime_event()")
_OLD_VALIDATOR_END = _OLD_GUARDS_SQL.index(
    "CREATE OR REPLACE FUNCTION messaging_validate_participant_read_cursor()"
)
RESTORE_OLD_REALTIME_VALIDATOR_SQL = _OLD_GUARDS_SQL[_OLD_VALIDATOR_START:_OLD_VALIDATOR_END]


class Migration(migrations.Migration):
    dependencies = [("messaging", "0009_messagereaction_messagerevision_message_deleted_at_and_more")]

    operations = [
        migrations.RunSQL(
            MUTATION_GUARDS_SQL,
            reverse_sql=(
                DROP_MUTATION_GUARDS_SQL
                + RESTORE_OLD_REALTIME_VALIDATOR_SQL
                + "\nDROP FUNCTION IF EXISTS messaging_role_principal_is_live(text, bigint, bigint);\n"
            ),
        )
    ]
