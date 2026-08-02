"""F3-5 — staff meetings: a manager schedules + invites staff; invitees RSVP; the
meeting is branch-scoped and surfaces on the invitee's upcoming list."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

MEET = "/api/v1/meetings/"


def _setup(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    t1 = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    t2 = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    return {
        "branch": branch,
        "manager": as_user(tenant, user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)),
        "t1": t1,
        "t1c": as_user(tenant, t1),
        "t2": t2,
        "t2c": as_user(tenant, t2),
    }


def _meeting_body(s, **over):
    start = timezone.now() + timedelta(days=1)
    body = {
        "title": "Weekly sync",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=1)).isoformat(),
        "branch": s["branch"].id,
        "attendees": [s["t1"].id],
    }
    body.update(over)
    return body


def test_schedule_invite_and_rsvp(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    created = s["manager"].post(MEET, _meeting_body(s), format="json")
    assert created.status_code == 201, created.content
    mid = created.json()["data"]["id"]
    assert created.json()["data"]["status"] == "scheduled"
    assert len(created.json()["data"]["attendees"]) == 1
    assert created.json()["data"]["attendees"][0]["response"] == "invited"

    # the invited teacher accepts
    resp = s["t1c"].post(f"{MEET}{mid}/respond/", {"response": "accepted"}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["attendees"][0]["response"] == "accepted"
    assert "user" not in resp.json()["data"]["attendees"][0]
    assert "principal" not in resp.json()["data"]["attendees"][0]
    assert "created_by" not in resp.json()["data"]


def test_manager_cancels(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    mid = s["manager"].post(MEET, _meeting_body(s), format="json").json()["data"]["id"]
    cancelled = s["manager"].post(f"{MEET}{mid}/cancel/", {}, format="json")
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    # Retries are idempotent and keep the original cancellation evidence.
    first_cancelled_at = cancelled.json()["data"]["cancelled_at"]
    retried = s["manager"].post(f"{MEET}{mid}/cancel/", {}, format="json")
    assert retried.status_code == 200
    assert retried.json()["data"]["cancelled_at"] == first_cancelled_at


def test_cannot_schedule_for_another_branch(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other = BranchFactory.create()
    cross = s["manager"].post(MEET, _meeting_body(s, branch=other.id), format="json")
    assert cross.status_code == 403
    assert cross.json()["code"] == "branch_out_of_scope"
    # a non-director must name a branch (no centre-wide)
    wide = s["manager"].post(MEET, _meeting_body(s, branch=None), format="json")
    assert wide.status_code == 403
    assert wide.json()["code"] == "branch_required"


def test_invitee_sees_meeting_others_dont(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    mid = s["manager"].post(MEET, _meeting_body(s), format="json").json()["data"]["id"]
    # the invited teacher sees it; the uninvited teacher does not
    assert s["t1c"].get(MEET).json()["pagination"]["total"] == 1
    assert s["t2c"].get(MEET).json()["pagination"]["total"] == 0
    # ...and it shows on the invitee's upcoming list
    upcoming = s["t1c"].get(f"{MEET}upcoming/").json()["data"]
    assert [m["id"] for m in upcoming] == [mid]


def test_non_invitee_cannot_rsvp(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    mid = s["manager"].post(MEET, _meeting_body(s), format="json").json()["data"]["id"]
    # t2 wasn't invited -> the meeting isn't in their scope -> 404
    assert s["t2c"].post(f"{MEET}{mid}/respond/", {"response": "accepted"}, format="json").status_code == 404


def test_shared_bridge_role_cannot_read_or_rsvp_another_principals_invite(tenant_a, client_for):
    from apps.meetings.models import MeetingAttendee, StaffMeeting
    from apps.org.tests.factories import BranchFactory
    from tests.role_principal_helpers import exact_session_client, shared_staff_teacher_bridge

    start = timezone.now() + timedelta(days=1)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        user, teacher, staff = shared_staff_teacher_bridge(
            branch=branch,
            staff_role=Role.SUPPORT,
        )
        meeting = StaffMeeting.objects.create(
            title="Teacher account only",
            branch=branch,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        MeetingAttendee.objects.create(
            meeting=meeting,
            user=user,
            principal_kind="teacher",
            principal_id=teacher.pk,
        )

    teacher_client = exact_session_client(
        client_for,
        tenant_a,
        user,
        principal_kind="teacher",
        principal_id=teacher.pk,
    )
    staff_client = exact_session_client(
        client_for,
        tenant_a,
        user,
        principal_kind="staff",
        principal_id=staff.pk,
    )
    assert teacher_client.get(f"{MEET}{meeting.pk}/").status_code == 200
    assert staff_client.get(f"{MEET}{meeting.pk}/").status_code == 404
    assert (
        staff_client.post(f"{MEET}{meeting.pk}/respond/", {"response": "accepted"}, format="json").status_code
        == 404
    )
    assert (
        teacher_client.post(
            f"{MEET}{meeting.pk}/respond/", {"response": "accepted"}, format="json"
        ).status_code
        == 200
    )


def test_teacher_cannot_schedule(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    # a teacher holds no meeting:write
    assert s["t1c"].post(MEET, _meeting_body(s), format="json").status_code == 403


def test_manager_invitee_can_rsvp_a_centre_wide_meeting(tenant_a, user_in, as_user, as_role):
    # a director schedules a centre-wide (no branch) meeting and invites an HOD manager
    s = _setup(tenant_a, user_in, as_user)
    director, _ = as_role(Role.DIRECTOR)
    hod_user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=s["branch"])
    start = timezone.now() + timedelta(days=1)
    mid = director.post(
        MEET,
        {
            "title": "All-staff",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            "attendees": [hod_user.id],
        },
        format="json",
    ).json()["data"]["id"]
    hod = as_user(tenant_a, hod_user)
    # the HOD is an invitee (a meeting:write holder) — they must be able to open AND RSVP
    # it even though it has no branch, not just see it in /upcoming/
    assert hod.get(f"{MEET}{mid}/").status_code == 200
    assert hod.post(f"{MEET}{mid}/respond/", {"response": "accepted"}, format="json").status_code == 200


def test_invitee_cannot_cancel(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    mid = s["manager"].post(MEET, _meeting_body(s), format="json").json()["data"]["id"]
    # t1 is an in-scope invitee (sees the meeting) but holds no meeting:write -> can't cancel
    assert s["t1c"].post(f"{MEET}{mid}/cancel/", {}, format="json").status_code == 403


def test_invalid_datetime_is_400_not_500(tenant_a, user_in, as_user):
    """A well-formed-but-invalid datetime (parse_datetime RAISES ValueError, not None)
    must be a clean 400, never a 500."""
    s = _setup(tenant_a, user_in, as_user)
    r = s["manager"].post(MEET, _meeting_body(s, starts_at="2026-02-30T10:00:00"), format="json")
    assert r.status_code == 400
    assert "starts_at" in r.json()["errors"]


def test_student_cannot_be_invited(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    student = user_in(tenant_a, roles=[Role.STUDENT], branch=s["branch"])
    # meetings are staff coordination — a student id is rejected by the attendee filter
    r = s["manager"].post(MEET, _meeting_body(s, attendees=[student.id]), format="json")
    assert r.status_code == 400


def test_ambiguous_staff_bridge_requires_role_native_invitee_selector(tenant_a, user_in, as_user):
    from tests.role_principal_helpers import shared_staff_teacher_bridge

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        ambiguous, teacher, _staff = shared_staff_teacher_bridge(
            branch=s["branch"],
            staff_role=Role.SUPPORT,
        )
    response = s["manager"].post(
        MEET,
        _meeting_body(s, attendees=[ambiguous.pk]),
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["errors"] == {
        "attendees": ["Choose active staff recipients in the meeting's scope."]
    }

    selected = s["manager"].post(
        MEET,
        _meeting_body(
            s,
            attendees=[],
            invitees=[{"kind": "teacher", "id": teacher.pk}],
        ),
        format="json",
    )
    assert selected.status_code == 201, selected.content
    [invitee] = selected.json()["data"]["attendees"]
    assert invitee["principal"]["kind"] == "teacher"
    assert invitee["principal"]["id"] == teacher.pk
    assert invitee["principal"]["display_name"]
    assert invitee["principal"]["account_label"] == "Teacher"


def test_next_meeting_for_surfaces_soonest(tenant_a, user_in, as_user):
    from apps.meetings.services import next_meeting_for, schedule_meeting

    s = _setup(tenant_a, user_in, as_user)
    now = timezone.now()
    with schema_context(tenant_a.schema_name):
        schedule_meeting(
            title="Later",
            starts_at=now + timedelta(days=5),
            ends_at=now + timedelta(days=5, hours=1),
            attendees=[s["t1"]],
            created_by=None,
            branch=s["branch"],
        )
        schedule_meeting(
            title="Sooner",
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=1, hours=1),
            attendees=[s["t1"]],
            created_by=None,
            branch=s["branch"],
        )
        nxt = next_meeting_for(s["t1"])
    assert nxt is not None
    assert nxt.title == "Sooner"  # the dashboard surfaces the soonest upcoming meeting


def test_rsvp_validation_and_read_only_impersonation_are_enforced(tenant_a, user_in, as_user, client_for):
    from apps.meetings.services import respond_to_meeting
    from core.exceptions import ValidationException
    from core.session_auth import create_session

    s = _setup(tenant_a, user_in, as_user)
    mid = s["manager"].post(MEET, _meeting_body(s), format="json").json()["data"]["id"]
    invalid = s["t1c"].post(f"{MEET}{mid}/respond/", {"response": "maybe"}, format="json")
    assert invalid.status_code == 400
    assert "response" in invalid.json()["errors"]
    with schema_context(tenant_a.schema_name), pytest.raises(ValidationException) as captured:
        respond_to_meeting(meeting_id=mid, user=s["t1"], response="maybe")
    assert captured.value.fields == {"response": ["Choose accepted or declined."]}

    with schema_context(tenant_a.schema_name):
        session = create_session(s["t1"], read_only=True)
    read_only = client_for(tenant_a)
    read_only.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")
    denied = read_only.post(f"{MEET}{mid}/respond/", {"response": "accepted"}, format="json")
    assert denied.status_code == 403
    assert denied.json()["code"] == "read_only_token"


def test_invited_manager_cannot_cancel_another_branch_or_centre_wide_meeting(
    tenant_a, user_in, as_user, as_role
):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
    hod_user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch_a)
    hod = as_user(tenant_a, hod_user)
    director, _ = as_role(Role.DIRECTOR)
    start = timezone.now() + timedelta(days=1)
    base = {
        "title": "Cross-scope",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=1)).isoformat(),
        "attendees": [hod_user.pk],
    }
    cross_id = director.post(MEET, {**base, "branch": branch_b.pk}, format="json").json()["data"]["id"]
    wide_id = director.post(MEET, base, format="json").json()["data"]["id"]

    cross = hod.post(f"{MEET}{cross_id}/cancel/", {}, format="json")
    assert cross.status_code == 403
    assert cross.json()["code"] == "branch_out_of_scope"
    wide = hod.post(f"{MEET}{wide_id}/cancel/", {}, format="json")
    assert wide.status_code == 403
    assert wide.json()["code"] == "branch_required"
    # Their invitation remains valid for RSVP even though cancellation is scoped.
    assert hod.post(f"{MEET}{wide_id}/respond/", {"response": "accepted"}, format="json").status_code == 200


def test_cross_branch_scope_is_checked_before_attendee_lookup(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        foreign_branch = BranchFactory()
    response = s["manager"].post(
        MEET,
        _meeting_body(s, branch=foreign_branch.pk, attendees=[999_999_999]),
        format="json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "branch_out_of_scope"


def test_meeting_filters_ordering_pagination_detail_and_head(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    first = s["manager"].post(MEET, _meeting_body(s, title="First"), format="json").json()["data"]
    second = (
        s["manager"]
        .post(
            MEET,
            _meeting_body(
                s,
                title="Second",
                starts_at=(timezone.now() + timedelta(days=2)).isoformat(),
                ends_at=(timezone.now() + timedelta(days=2, hours=1)).isoformat(),
            ),
            format="json",
        )
        .json()["data"]
    )
    s["manager"].post(f"{MEET}{second['id']}/cancel/", {}, format="json")

    filtered = s["manager"].get(
        f"{MEET}?status=cancelled&branch={s['branch'].pk}&ordering=-starts_at&page=1&page_size=1"
    )
    assert filtered.status_code == 200
    assert [row["id"] for row in filtered.json()["data"]] == [second["id"]]
    assert s["manager"].get(f"{MEET}?status=garbage").status_code == 400
    assert s["manager"].get(f"{MEET}?branch=garbage").status_code == 400
    missing_branch = s["manager"].get(f"{MEET}?branch=999999999")
    assert missing_branch.status_code == 200
    assert missing_branch.json()["pagination"]["total"] == 0
    invalid_ordering = s["manager"].get(f"{MEET}?ordering=--starts_at")
    assert invalid_ordering.status_code == 400
    assert invalid_ordering.json()["errors"] == {"ordering": ["Choose starts_at or -starts_at."]}

    detail = s["manager"].get(f"{MEET}{first['id']}/")
    assert detail.status_code == 200
    assert detail.json()["data"]["title"] == "First"
    assert s["manager"].head(MEET).status_code == 200
    assert s["manager"].head(f"{MEET}{first['id']}/").status_code == 200
    assert s["t1c"].head(f"{MEET}upcoming/").status_code == 200


def test_meeting_text_fields_trim_and_blank_title_is_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    created = s["manager"].post(
        MEET,
        _meeting_body(s, title="  Weekly sync  ", agenda="  Agenda  ", location="  Room 1  "),
        format="json",
    )
    assert created.status_code == 201, created.content
    data = created.json()["data"]
    assert data["title"] == "Weekly sync"
    assert data["agenda"] == "Agenda"
    assert data["location"] == "Room 1"
    assert s["manager"].post(MEET, _meeting_body(s, title="   "), format="json").status_code == 400


def test_meeting_unknown_fields_time_bounds_and_attendee_limit_are_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    assert (
        s["manager"]
        .post(
            MEET,
            _meeting_body(s, surprise=True),
            format="json",
        )
        .status_code
        == 400
    )
    assert s["manager"].get(f"{MEET}?surprise=true").status_code == 400

    start = timezone.now() + timedelta(days=1)
    too_long = s["manager"].post(
        MEET,
        _meeting_body(s, starts_at=start.isoformat(), ends_at=(start + timedelta(hours=25)).isoformat()),
        format="json",
    )
    assert too_long.status_code == 400
    assert set(too_long.json()["errors"]) == {"ends_at"}
    too_many = s["manager"].post(
        MEET,
        _meeting_body(s, attendees=list(range(1, 202))),
        format="json",
    )
    assert too_many.status_code == 400
    assert set(too_many.json()["errors"]) == {"attendees"}


def test_department_only_meeting_write_does_not_expand_to_the_whole_branch(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
    manager_user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.filter(user=manager_user, branch=branch).update(department=department)
    manager = as_user(tenant_a, manager_user)
    start = timezone.now() + timedelta(days=1)
    response = manager.post(
        MEET,
        {
            "title": "Must carry department scope",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            "branch": branch.pk,
            "attendees": [teacher_user.pk],
        },
        format="json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "branch_out_of_scope"


def test_cancel_wins_before_late_rsvp(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    mid = s["manager"].post(MEET, _meeting_body(s), format="json").json()["data"]["id"]
    assert s["manager"].post(f"{MEET}{mid}/cancel/", {}, format="json").status_code == 200
    response = s["t1c"].post(f"{MEET}{mid}/respond/", {"response": "accepted"}, format="json")
    assert response.status_code == 422
    assert response.json()["code"] == "meeting_not_scheduled"


def test_meeting_presentation_has_readable_links_and_actor_snapshots(
    tenant_a,
    user_in,
    as_user,
    django_assert_num_queries,
):
    from apps.meetings.models import StaffMeeting
    from apps.meetings.presenters import meeting_to_dict
    from apps.meetings.repositories.meeting_repository import MeetingRepository

    s = _setup(tenant_a, user_in, as_user)
    created = s["manager"].post(MEET, _meeting_body(s), format="json")
    assert created.status_code == 201, created.content
    data = created.json()["data"]
    assert data["branch"] == s["branch"].pk
    assert data["branch_name"] == s["branch"].name
    assert data["created_by"]["kind"] == "staff"
    assert data["created_by"]["display_name"]
    assert data["attendees"][0]["principal"]["kind"] == "teacher"
    assert data["attendees"][0]["principal"]["display_name"]

    cancelled = s["manager"].post(f"{MEET}{data['id']}/cancel/", {}, format="json")
    assert cancelled.status_code == 200, cancelled.content
    assert cancelled.json()["data"]["cancelled_by"]["kind"] == "staff"
    with schema_context(tenant_a.schema_name):
        meeting = StaffMeeting.objects.get(pk=data["id"])
        assert meeting.created_by_principal_kind == "staff"
        assert meeting.created_by_principal_id is not None
        assert meeting.cancelled_by_principal_kind == "staff"
        assert meeting.cancelled_by_principal_id == meeting.created_by_principal_id
        # One meeting query plus one bounded attendee prefetch; readable role
        # names never create an N+1 lookup per invitee.
        with django_assert_num_queries(2):
            [loaded] = list(MeetingRepository().get_queryset())
            presented = meeting_to_dict(loaded)
        assert presented["created_by"]["display_name"]
        assert presented["attendees"][0]["principal"]["display_name"]


def test_meeting_principal_guards_reject_wrong_owner_and_rewrite(tenant_a, user_in):
    from django.db import DatabaseError, transaction

    from apps.meetings.models import MeetingAttendee, StaffMeeting
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        first = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
        second = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
        meeting = StaffMeeting.objects.create(
            title="Guarded",
            branch=branch,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=1),
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            MeetingAttendee.objects.bulk_create(
                [
                    MeetingAttendee(
                        meeting=meeting,
                        user=first,
                        principal_kind="teacher",
                        principal_id=second.test_principal_id,
                    )
                ]
            )
        attendee = MeetingAttendee.objects.create(
            meeting=meeting,
            user=first,
            principal_kind="teacher",
            principal_id=first.test_principal_id,
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            MeetingAttendee.objects.filter(pk=attendee.pk).update(principal_id=second.test_principal_id)


def test_historical_creator_deactivation_does_not_freeze_meeting_lifecycle(
    tenant_a,
    user_in,
    as_user,
):
    """Attribution proves ownership; it must not require the actor to remain active forever."""
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    creator = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    reviewer = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    invitee = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    creator_client = as_user(tenant_a, creator)
    reviewer_client = as_user(tenant_a, reviewer)
    start = timezone.now() + timedelta(days=1)
    created = creator_client.post(
        MEET,
        {
            "title": "Historical actor",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            "branch": branch.pk,
            "attendees": [invitee.pk],
        },
        format="json",
    )
    assert created.status_code == 201, created.content

    with schema_context(tenant_a.schema_name):
        StaffProfile.objects.filter(pk=creator.test_principal_id).update(is_active=False)

    cancelled = reviewer_client.post(
        f"{MEET}{created.json()['data']['id']}/cancel/",
        {},
        format="json",
    )
    assert cancelled.status_code == 200, cancelled.content
    assert cancelled.json()["data"]["status"] == "cancelled"


def test_meeting_actor_snapshots_are_immutable_and_survive_bridge_deletion(
    tenant_a,
    user_in,
    as_user,
    django_assert_num_queries,
):
    from django.db import DatabaseError, transaction

    from apps.meetings.models import StaffMeeting
    from apps.meetings.presenters import meeting_to_dict
    from apps.meetings.repositories.meeting_repository import MeetingRepository

    s = _setup(tenant_a, user_in, as_user)
    created = s["manager"].post(MEET, _meeting_body(s), format="json")
    assert created.status_code == 201, created.content
    meeting_id = created.json()["data"]["id"]
    cancelled = s["manager"].post(f"{MEET}{meeting_id}/cancel/", {}, format="json")
    assert cancelled.status_code == 200, cancelled.content
    assert cancelled.json()["data"]["created_by_attribution_status"] == "captured"
    assert cancelled.json()["data"]["cancelled_by_attribution_status"] == "captured"

    with schema_context(tenant_a.schema_name):
        meeting = StaffMeeting.objects.get(pk=meeting_id)
        creator_kind = meeting.created_by_principal_kind
        creator_id = meeting.created_by_principal_id
        creator_user_id = meeting.created_by_id
        with pytest.raises(DatabaseError), transaction.atomic():
            StaffMeeting.objects.filter(pk=meeting_id).update(
                created_by_principal_id=s["t1"].test_principal_id
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            StaffMeeting.objects.filter(pk=meeting_id).update(cancelled_by_attribution_status="quarantined")

        from apps.users.models import User

        User.objects.get(pk=creator_user_id).delete()
        with django_assert_num_queries(2):
            loaded = MeetingRepository().get_queryset().get(pk=meeting_id)
            payload = meeting_to_dict(loaded)
        assert loaded.created_by_id is None
        assert loaded.cancelled_by_id is None
        assert payload["created_by"] == {
            "kind": creator_kind,
            "id": creator_id,
            "display_name": None,
            "account_label": "Staff",
        }
        assert payload["cancelled_by"] == payload["created_by"]
        assert payload["created_by_attribution_status"] == "captured"
        assert payload["cancelled_by_attribution_status"] == "captured"
