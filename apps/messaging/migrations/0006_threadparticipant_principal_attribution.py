from __future__ import annotations

from django.db import migrations, models

_PROFILE_MODELS = (
    ("student", "students", "StudentProfile"),
    ("teacher", "teachers", "TeacherProfile"),
    ("parent", "parents", "ParentProfile"),
    ("staff", "org", "StaffProfile"),
)


def backfill_participant_principals(apps, schema_editor):
    """Resolve only rows with one active role-account owner.

    Ambiguous rows are classified for review and remain invisible. Rows with no
    evidence stay unresolved. The session-local maintenance marker also lets this
    function be rerun deliberately after the immutability trigger exists.
    """

    Participant = apps.get_model("messaging", "ThreadParticipant")
    User = apps.get_model("users", "User")
    database = schema_editor.connection.alias
    last_pk = 0
    while True:
        rows = list(
            Participant.objects.using(database).filter(
                pk__gt=last_pk,
                attribution_status__in=("unresolved", "conflicting", "quarantined"),
            ).order_by("pk")[:500]
        )
        if not rows:
            return
        last_pk = rows[-1].pk
        user_ids = {row.user_id for row in rows}
        active_user_ids = set(
            User.objects.using(database)
            .filter(pk__in=user_ids, is_active=True)
            .values_list("pk", flat=True)
        )
        evidence: dict[int, list[tuple[str, int]]] = {user_id: [] for user_id in user_ids}
        for kind, app_label, model_name in _PROFILE_MODELS:
            Profile = apps.get_model(app_label, model_name)
            for principal_id, user_id in Profile.objects.using(database).filter(
                user_id__in=active_user_ids,
                is_active=True,
            ).values_list("pk", "user_id"):
                evidence[int(user_id)].append((kind, int(principal_id)))

        for row in rows:
            candidates = evidence[row.user_id]
            if len(candidates) == 1:
                row.principal_kind, row.principal_id = candidates[0]
                row.attribution_status = "resolved"
            elif len(candidates) > 1:
                row.principal_kind = None
                row.principal_id = None
                row.attribution_status = "conflicting"
            else:
                row.principal_kind = None
                row.principal_id = None
                row.attribution_status = "unresolved"

        with schema_editor.connection.cursor() as cursor:
            cursor.execute("SET LOCAL starforge.messaging_maintenance = 'principal-backfill'")
        Participant.objects.using(database).bulk_update(
            rows,
            ("principal_kind", "principal_id", "attribution_status"),
            batch_size=500,
        )


PARTICIPANT_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION messaging_guard_participant_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.user_id IS NOT DISTINCT FROM OLD.user_id
       AND NEW.principal_kind IS NOT DISTINCT FROM OLD.principal_kind
       AND NEW.principal_id IS NOT DISTINCT FROM OLD.principal_id
       AND NEW.attribution_status IS NOT DISTINCT FROM OLD.attribution_status THEN
        RETURN NEW;
    END IF;

    IF current_setting('starforge.messaging_maintenance', true) = 'principal-backfill'
       AND OLD.attribution_status IN ('unresolved', 'conflicting', 'quarantined')
       AND (
            (
                NEW.attribution_status = 'resolved'
                AND NEW.principal_kind IN ('student', 'teacher', 'parent', 'staff')
                AND NEW.principal_id IS NOT NULL
            )
            OR
            (
                NEW.attribution_status IN ('unresolved', 'conflicting', 'quarantined')
                AND NEW.principal_kind IS NULL
                AND NEW.principal_id IS NULL
            )
       )
       AND (((to_jsonb(NEW) - 'principal_kind') - 'principal_id') - 'attribution_status')
           = (((to_jsonb(OLD) - 'principal_kind') - 'principal_id') - 'attribution_status') THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'messaging participant attribution is immutable'
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION messaging_validate_participant_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    principal_is_live boolean := false;
BEGIN
    IF NEW.attribution_status NOT IN ('captured', 'resolved') THEN
        RETURN NEW;
    END IF;

    CASE NEW.principal_kind
        WHEN 'student' THEN
            SELECT EXISTS (
                SELECT 1 FROM students_studentprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                 WHERE principal.id = NEW.principal_id
                   AND principal.user_id = NEW.user_id
                   AND principal.is_active
                   AND bridge_user.is_active
            ) INTO principal_is_live;
        WHEN 'teacher' THEN
            SELECT EXISTS (
                SELECT 1 FROM teachers_teacherprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                 WHERE principal.id = NEW.principal_id
                   AND principal.user_id = NEW.user_id
                   AND principal.is_active
                   AND bridge_user.is_active
            ) INTO principal_is_live;
        WHEN 'parent' THEN
            SELECT EXISTS (
                SELECT 1 FROM parents_parentprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                 WHERE principal.id = NEW.principal_id
                   AND principal.user_id = NEW.user_id
                   AND principal.is_active
                   AND bridge_user.is_active
            ) INTO principal_is_live;
        WHEN 'staff' THEN
            SELECT EXISTS (
                SELECT 1 FROM org_staffprofile principal
                JOIN users_user bridge_user ON bridge_user.id = principal.user_id
                 WHERE principal.id = NEW.principal_id
                   AND principal.user_id = NEW.user_id
                   AND principal.is_active
                   AND bridge_user.is_active
            ) INTO principal_is_live;
        ELSE
            principal_is_live := false;
    END CASE;

    IF NOT principal_is_live THEN
        RAISE EXCEPTION 'messaging participant principal is not active or owned by user'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS messaging_participant_snapshot_immutable
ON messaging_threadparticipant;
CREATE TRIGGER messaging_participant_snapshot_immutable
BEFORE UPDATE ON messaging_threadparticipant
FOR EACH ROW EXECUTE FUNCTION messaging_guard_participant_snapshot();

DROP TRIGGER IF EXISTS messaging_participant_snapshot_valid_insert
ON messaging_threadparticipant;
CREATE TRIGGER messaging_participant_snapshot_valid_insert
BEFORE INSERT ON messaging_threadparticipant
FOR EACH ROW EXECUTE FUNCTION messaging_validate_participant_snapshot();

DROP TRIGGER IF EXISTS messaging_participant_snapshot_valid_update
ON messaging_threadparticipant;
CREATE TRIGGER messaging_participant_snapshot_valid_update
BEFORE UPDATE OF user_id, principal_kind, principal_id, attribution_status
ON messaging_threadparticipant
FOR EACH ROW EXECUTE FUNCTION messaging_validate_participant_snapshot();
"""


DROP_PARTICIPANT_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS messaging_participant_snapshot_valid_update
ON messaging_threadparticipant;
DROP TRIGGER IF EXISTS messaging_participant_snapshot_valid_insert
ON messaging_threadparticipant;
DROP TRIGGER IF EXISTS messaging_participant_snapshot_immutable
ON messaging_threadparticipant;
DROP FUNCTION IF EXISTS messaging_validate_participant_snapshot();
DROP FUNCTION IF EXISTS messaging_guard_participant_snapshot();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("messaging", "0005_uploadgrant_source_cleanup"),
        ("students", "0011_protect_identity_history"),
        ("teachers", "0010_alter_payoutpolicy_method"),
        ("parents", "0010_preserve_family_lifecycle_history"),
        ("org", "0021_durable_center_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="threadparticipant",
            name="principal_kind",
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
            model_name="threadparticipant",
            name="principal_id",
            field=models.PositiveBigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="threadparticipant",
            name="attribution_status",
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
        migrations.RunPython(backfill_participant_principals, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="threadparticipant",
            index=models.Index(
                fields=["principal_kind", "principal_id", "thread"],
                name="msg_part_principal_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="threadparticipant",
            index=models.Index(
                fields=["attribution_status", "thread"],
                name="message_participant_review_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="threadparticipant",
            constraint=models.UniqueConstraint(
                condition=models.Q(attribution_status__in=("captured", "resolved")),
                fields=("thread", "principal_kind", "principal_id"),
                name="one_participation_per_thread_principal",
            ),
        ),
        migrations.AddConstraint(
            model_name="threadparticipant",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status__in=("captured", "resolved"),
                        principal_kind__in=("student", "teacher", "parent", "staff"),
                        principal_id__isnull=False,
                    )
                    | models.Q(
                        attribution_status__in=("unresolved", "conflicting", "quarantined"),
                        principal_kind__isnull=True,
                        principal_id__isnull=True,
                    )
                ),
                name="message_participant_attribution_shape",
            ),
        ),
        migrations.RunSQL(PARTICIPANT_GUARDS_SQL, DROP_PARTICIPANT_GUARDS_SQL),
    ]
