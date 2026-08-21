from __future__ import annotations

from django.db import migrations, models


def backfill_attendee_principals(apps, schema_editor):
    MeetingAttendee = apps.get_model("meetings", "MeetingAttendee")
    StaffProfile = apps.get_model("org", "StaffProfile")
    TeacherProfile = apps.get_model("teachers", "TeacherProfile")
    database = schema_editor.connection.alias

    last_pk = 0
    while True:
        rows = list(
            MeetingAttendee.objects.using(database)
            .filter(pk__gt=last_pk, principal_id=None)
            .order_by("pk")[:500]
        )
        if not rows:
            break
        last_pk = rows[-1].pk
        user_ids = {row.user_id for row in rows}
        evidence: dict[int, list[tuple[str, int]]] = {user_id: [] for user_id in user_ids}
        for kind, model in (("staff", StaffProfile), ("teacher", TeacherProfile)):
            for user_id, principal_id in model.objects.using(database).filter(
                user_id__in=user_ids,
                user__is_active=True,
                is_active=True,
            ).values_list("user_id", "pk"):
                evidence[user_id].append((kind, principal_id))
        changed = []
        for attendee in rows:
            candidates = evidence[attendee.user_id]
            if len(candidates) != 1:
                continue
            attendee.principal_kind, attendee.principal_id = candidates[0]
            changed.append(attendee)
        if changed:
            MeetingAttendee.objects.using(database).bulk_update(
                changed,
                ["principal_kind", "principal_id"],
                batch_size=500,
            )


def backfill_meeting_actor_principals(apps, schema_editor):
    StaffMeeting = apps.get_model("meetings", "StaffMeeting")
    StaffProfile = apps.get_model("org", "StaffProfile")
    TeacherProfile = apps.get_model("teachers", "TeacherProfile")
    database = schema_editor.connection.alias
    last_pk = 0
    while True:
        rows = list(
            StaffMeeting.objects.using(database).filter(pk__gt=last_pk).order_by("pk")[:500]
        )
        if not rows:
            break
        last_pk = rows[-1].pk
        user_ids = {
            user_id
            for row in rows
            for user_id in (row.created_by_id, row.cancelled_by_id)
            if user_id is not None
        }
        evidence: dict[int, list[tuple[str, int]]] = {user_id: [] for user_id in user_ids}
        for kind, model in (("staff", StaffProfile), ("teacher", TeacherProfile)):
            for user_id, principal_id in model.objects.using(database).filter(
                user_id__in=user_ids,
                user__is_active=True,
                is_active=True,
            ).values_list("user_id", "pk"):
                evidence[user_id].append((kind, principal_id))
        changed = []
        for meeting in rows:
            creator = evidence.get(meeting.created_by_id, [])
            canceller = evidence.get(meeting.cancelled_by_id, [])
            if len(creator) == 1:
                meeting.created_by_principal_kind, meeting.created_by_principal_id = creator[0]
                meeting.created_by_attribution_status = "resolved"
            else:
                meeting.created_by_principal_kind = ""
                meeting.created_by_principal_id = None
                meeting.created_by_attribution_status = "quarantined"
            if len(canceller) == 1:
                meeting.cancelled_by_principal_kind, meeting.cancelled_by_principal_id = canceller[0]
                meeting.cancelled_by_attribution_status = "resolved"
            elif meeting.cancelled_by_id is None and meeting.status != "cancelled":
                meeting.cancelled_by_principal_kind = ""
                meeting.cancelled_by_principal_id = None
                meeting.cancelled_by_attribution_status = "not_applicable"
            else:
                meeting.cancelled_by_principal_kind = ""
                meeting.cancelled_by_principal_id = None
                meeting.cancelled_by_attribution_status = "quarantined"
            changed.append(meeting)
        if changed:
            StaffMeeting.objects.using(database).bulk_update(
                changed,
                [
                    "created_by_principal_kind",
                    "created_by_principal_id",
                    "created_by_attribution_status",
                    "cancelled_by_principal_kind",
                    "cancelled_by_principal_id",
                    "cancelled_by_attribution_status",
                ],
                batch_size=500,
            )


MEETING_PRINCIPAL_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION meetings_principal_is_owned(
    principal_kind text,
    principal_id bigint,
    bridge_user_id bigint
) RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    IF principal_kind = 'staff' THEN
        RETURN EXISTS (
            SELECT 1 FROM org_staffprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = principal_id AND profile.user_id = bridge_user_id
              AND profile.is_active AND bridge.is_active
        );
    ELSIF principal_kind = 'teacher' THEN
        RETURN EXISTS (
            SELECT 1 FROM teachers_teacherprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = principal_id AND profile.user_id = bridge_user_id
              AND profile.is_active AND bridge.is_active
        );
    END IF;
    RETURN false;
END;
$$;

CREATE OR REPLACE FUNCTION meetings_guard_attendee_principal()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    principal_changed boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        principal_changed := true;
    ELSE
        principal_changed := (
            NEW.user_id IS DISTINCT FROM OLD.user_id
            OR NEW.principal_kind IS DISTINCT FROM OLD.principal_kind
            OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
        );
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.user_id IS DISTINCT FROM OLD.user_id
        OR NEW.principal_kind IS DISTINCT FROM OLD.principal_kind
        OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
    ) THEN
        RAISE EXCEPTION 'meeting attendee principal attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF principal_changed AND NEW.principal_id IS NOT NULL
       AND NOT meetings_principal_is_owned(NEW.principal_kind, NEW.principal_id, NEW.user_id) THEN
        RAISE EXCEPTION 'meeting attendee principal is not owned by user'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION meetings_guard_actor_principals()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    creator_changed boolean := false;
    canceller_changed boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        creator_changed := true;
        canceller_changed := true;
    ELSE
        creator_changed := (
            NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
            OR NEW.created_by_principal_kind IS DISTINCT FROM OLD.created_by_principal_kind
            OR NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
            OR NEW.created_by_attribution_status IS DISTINCT FROM OLD.created_by_attribution_status
        );
        canceller_changed := (
            NEW.cancelled_by_id IS DISTINCT FROM OLD.cancelled_by_id
            OR NEW.cancelled_by_principal_kind IS DISTINCT FROM OLD.cancelled_by_principal_kind
            OR NEW.cancelled_by_principal_id IS DISTINCT FROM OLD.cancelled_by_principal_id
            OR NEW.cancelled_by_attribution_status IS DISTINCT FROM OLD.cancelled_by_attribution_status
        );
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.created_by_principal_kind IS DISTINCT FROM OLD.created_by_principal_kind
        OR NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
        OR NEW.created_by_attribution_status IS DISTINCT FROM OLD.created_by_attribution_status
    ) THEN
        RAISE EXCEPTION 'meeting creator principal attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
       AND NOT (OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL) THEN
        RAISE EXCEPTION 'meeting creator bridge attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF creator_changed AND NEW.created_by_attribution_status IN ('captured', 'resolved')
       AND NEW.created_by_id IS NOT NULL
       AND NOT meetings_principal_is_owned(
           NEW.created_by_principal_kind, NEW.created_by_principal_id, NEW.created_by_id
       ) THEN
        RAISE EXCEPTION 'meeting creator principal is not owned by user'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.cancelled_by_principal_kind IS DISTINCT FROM OLD.cancelled_by_principal_kind
        OR NEW.cancelled_by_principal_id IS DISTINCT FROM OLD.cancelled_by_principal_id
        OR NEW.cancelled_by_attribution_status IS DISTINCT FROM OLD.cancelled_by_attribution_status
    ) AND NOT (
        OLD.cancelled_by_id IS NULL
        AND OLD.cancelled_by_principal_kind = ''
        AND OLD.cancelled_by_principal_id IS NULL
        AND OLD.cancelled_by_attribution_status = 'not_applicable'
        AND NEW.cancelled_by_id IS NOT NULL
        AND NEW.cancelled_by_principal_kind IN ('staff', 'teacher')
        AND NEW.cancelled_by_principal_id IS NOT NULL
        AND NEW.cancelled_by_attribution_status = 'captured'
        AND NEW.status = 'cancelled'
        AND NEW.cancelled_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'meeting canceller principal attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.cancelled_by_id IS DISTINCT FROM OLD.cancelled_by_id
       AND NOT (
           (OLD.cancelled_by_id IS NULL
            AND OLD.cancelled_by_attribution_status = 'not_applicable'
            AND NEW.cancelled_by_id IS NOT NULL
            AND NEW.cancelled_by_attribution_status = 'captured'
            AND NEW.status = 'cancelled')
           OR (OLD.cancelled_by_id IS NOT NULL AND NEW.cancelled_by_id IS NULL)
       ) THEN
        RAISE EXCEPTION 'meeting canceller bridge attribution is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF canceller_changed AND NEW.cancelled_by_attribution_status IN ('captured', 'resolved')
       AND NEW.cancelled_by_id IS NOT NULL
       AND NOT meetings_principal_is_owned(
           NEW.cancelled_by_principal_kind, NEW.cancelled_by_principal_id, NEW.cancelled_by_id
       ) THEN
        RAISE EXCEPTION 'meeting canceller principal is not owned by user'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER meetings_attendee_principal_guard
BEFORE INSERT OR UPDATE ON meetings_meetingattendee
FOR EACH ROW EXECUTE FUNCTION meetings_guard_attendee_principal();

CREATE TRIGGER meetings_actor_principal_guard
BEFORE INSERT OR UPDATE ON meetings_staffmeeting
FOR EACH ROW EXECUTE FUNCTION meetings_guard_actor_principals();
"""


DROP_MEETING_PRINCIPAL_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS meetings_actor_principal_guard ON meetings_staffmeeting;
DROP TRIGGER IF EXISTS meetings_attendee_principal_guard ON meetings_meetingattendee;
DROP FUNCTION IF EXISTS meetings_guard_actor_principals();
DROP FUNCTION IF EXISTS meetings_guard_attendee_principal();
DROP FUNCTION IF EXISTS meetings_principal_is_owned(text, bigint, bigint);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("meetings", "0001_initial"),
        ("org", "0021_durable_center_settings"),
        ("teachers", "0010_alter_payoutpolicy_method"),
    ]

    operations = [
        migrations.AddField(
            model_name="meetingattendee",
            name="principal_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="meetingattendee",
            name="principal_kind",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="staffmeeting",
            name="created_by_principal_kind",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="staffmeeting",
            name="created_by_attribution_status",
            field=models.CharField(
                choices=(
                    ("captured", "Captured"),
                    ("resolved", "Resolved from legacy data"),
                    ("quarantined", "Quarantined"),
                    ("not_applicable", "Not applicable"),
                ),
                default="quarantined",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="staffmeeting",
            name="created_by_principal_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="staffmeeting",
            name="cancelled_by_principal_kind",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="staffmeeting",
            name="cancelled_by_principal_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="staffmeeting",
            name="cancelled_by_attribution_status",
            field=models.CharField(
                choices=(
                    ("captured", "Captured"),
                    ("resolved", "Resolved from legacy data"),
                    ("quarantined", "Quarantined"),
                    ("not_applicable", "Not applicable"),
                ),
                default="not_applicable",
                max_length=16,
            ),
        ),
        migrations.RunPython(backfill_attendee_principals, migrations.RunPython.noop),
        migrations.RunPython(backfill_meeting_actor_principals, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="meetingattendee",
            constraint=models.UniqueConstraint(
                condition=models.Q(principal_id__isnull=False),
                fields=("meeting", "principal_kind", "principal_id"),
                name="one_invite_per_meeting_principal",
            ),
        ),
        migrations.AddConstraint(
            model_name="meetingattendee",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(principal_kind="", principal_id__isnull=True)
                    | (
                        models.Q(principal_kind__in=("staff", "teacher"))
                        & models.Q(principal_id__isnull=False)
                    )
                ),
                name="meeting_attendee_principal_pair",
            ),
        ),
        migrations.AddIndex(
            model_name="meetingattendee",
            index=models.Index(
                fields=["principal_kind", "principal_id", "meeting"],
                name="meeting_attendee_principal_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="staffmeeting",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(created_by_principal_kind__in=("staff", "teacher"))
                        & models.Q(created_by_principal_id__isnull=False)
                        & models.Q(created_by_attribution_status__in=("captured", "resolved"))
                    )
                    | models.Q(
                        created_by_principal_kind="",
                        created_by_principal_id__isnull=True,
                        created_by_attribution_status="quarantined",
                    )
                ),
                name="meeting_creator_principal_pair",
            ),
        ),
        migrations.AddConstraint(
            model_name="staffmeeting",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(cancelled_by_principal_kind__in=("staff", "teacher"))
                        & models.Q(cancelled_by_principal_id__isnull=False)
                        & models.Q(cancelled_by_attribution_status__in=("captured", "resolved"))
                        & models.Q(status="cancelled")
                        & models.Q(cancelled_at__isnull=False)
                    )
                    | models.Q(
                        cancelled_by_principal_kind="",
                        cancelled_by_principal_id__isnull=True,
                        cancelled_by_attribution_status="not_applicable",
                        cancelled_by__isnull=True,
                        cancelled_at__isnull=True,
                        status="scheduled",
                    )
                    | models.Q(
                        cancelled_by_principal_kind="",
                        cancelled_by_principal_id__isnull=True,
                        cancelled_by_attribution_status="quarantined",
                    )
                ),
                name="meeting_canceller_principal_pair",
            ),
        ),
        migrations.RunSQL(MEETING_PRINCIPAL_GUARDS_SQL, DROP_MEETING_PRINCIPAL_GUARDS_SQL),
    ]
