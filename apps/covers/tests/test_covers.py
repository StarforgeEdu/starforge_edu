"""F18-1 — lesson cover requests: request → assign / open-pool → claim, with the
lesson actually reassigned to the cover teacher (conflict-handled), plus guards."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

COVER = "/api/v1/cover/"


def _setup(tenant, user_in, as_user):
    """A branch with teacher A (owns a lesson), teacher B (potential cover), and a
    branch manager. Returns the clients + the relevant ids."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    a_user = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    b_user = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    start = timezone.now() + timedelta(days=1)
    with schema_context(tenant.schema_name):
        a_prof = TeacherProfileFactory.create(user=a_user, branch=branch)
        b_prof = TeacherProfileFactory.create(user=b_user, branch=branch)
        cohort = CohortFactory.create(branch=branch)
        term = TermFactory.create()
        lesson = Lesson.objects.create(
            term=term,
            cohort=cohort,
            teacher=a_prof,
            title="Algebra",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
    return {
        "branch": branch,
        "a_client": as_user(tenant, a_user),
        "b_client": as_user(tenant, b_user),
        "manager": as_user(tenant, user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)),
        "a_prof": a_prof,
        "b_prof": b_prof,
        "term": term,
        "lesson": lesson,
        "start": start,
    }


def test_request_then_assign_reassigns_the_lesson(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    req = s["a_client"].post(COVER, {"lesson": s["lesson"].id, "reason": "sick"}, format="json")
    assert req.status_code == 201, req.content
    assert req.json()["data"]["status"] == "open"
    cid = req.json()["data"]["id"]

    approved = s["manager"].post(f"{COVER}{cid}/assign/", {"cover_teacher": s["b_prof"].id}, format="json")
    assert approved.status_code == 200, approved.content
    assert approved.json()["data"]["status"] == "approved"

    with schema_context(tenant_a.schema_name):
        s["lesson"].refresh_from_db()
        assert s["lesson"].teacher_id == s["b_prof"].id  # the cover actually took effect


def test_assign_to_an_offboarded_teacher_is_rejected(tenant_a, user_in, as_user):
    """R3/CONF1: assigning a cover to an offboarded teacher (login disabled) would
    reassign a live lesson to someone who can't act on it. The assign path must reject
    an inactive teacher, matching the pool path (which only surfaces active staff)."""
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id, "reason": "sick"}, format="json").json()[
        "data"
    ]["id"]
    # Offboard teacher B: disable their login (the TeacherProfile row survives).
    with schema_context(tenant_a.schema_name):
        b_user = s["b_prof"].user
        b_user.is_active = False
        b_user.save(update_fields=["is_active"])
    resp = s["manager"].post(f"{COVER}{cid}/assign/", {"cover_teacher": s["b_prof"].id}, format="json")
    assert resp.status_code == 400, resp.content
    assert resp.json()["code"] == "validation_error"


def test_pool_then_claim_reassigns(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    assert s["manager"].post(f"{COVER}{cid}/open-pool/", {}, format="json").json()["data"]["pool"] is True
    claimed = s["b_client"].post(f"{COVER}{cid}/claim/", {}, format="json")
    assert claimed.status_code == 200
    assert claimed.json()["data"]["status"] == "approved"
    with schema_context(tenant_a.schema_name):
        s["lesson"].refresh_from_db()
        assert s["lesson"].teacher_id == s["b_prof"].id


def test_assign_to_a_busy_teacher_conflicts(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson

    s = _setup(tenant_a, user_in, as_user)
    # B already teaches a different cohort's lesson at the SAME time
    with schema_context(tenant_a.schema_name):
        other = CohortFactory.create(branch=s["branch"])
        Lesson.objects.create(
            term=s["term"],
            cohort=other,
            teacher=s["b_prof"],
            title="B busy",
            starts_at=s["start"],
            ends_at=s["start"] + timedelta(hours=1),
        )
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    conflict = s["manager"].post(f"{COVER}{cid}/assign/", {"cover_teacher": s["b_prof"].id}, format="json")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "cover_conflict"
    # nothing changed: lesson still A, request still open
    with schema_context(tenant_a.schema_name):
        s["lesson"].refresh_from_db()
        assert s["lesson"].teacher_id == s["a_prof"].id
        from apps.covers.models import CoverRequest

        assert CoverRequest.objects.get(pk=cid).status == "open"


def test_cannot_request_cover_for_a_lesson_you_dont_teach(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    # teacher B doesn't teach A's lesson
    r = s["b_client"].post(COVER, {"lesson": s["lesson"].id}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "not_lesson_teacher"


def test_cannot_cover_self(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    # assigning the original teacher as their own cover is rejected
    r = s["manager"].post(f"{COVER}{cid}/assign/", {"cover_teacher": s["a_prof"].id}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "cant_cover_self"


def test_requester_cancels_and_duplicate_blocked(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    # a duplicate live request for the same lesson is blocked
    dup = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json")
    assert dup.status_code == 409
    assert dup.json()["code"] == "cover_already_requested"
    # the requester cancels their own; afterwards a new request is allowed again
    assert (
        s["a_client"].post(f"{COVER}{cid}/cancel/", {}, format="json").json()["data"]["status"] == "cancelled"
    )
    again = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json")
    assert again.status_code == 201


def test_manager_rejects(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    rejected = s["manager"].post(f"{COVER}{cid}/reject/", {}, format="json")
    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == "rejected"


def test_cover_teacher_must_be_in_branch(tenant_a, user_in, as_user):
    """A branch manager cannot pull a teacher from ANOTHER branch onto this branch's
    lesson — the cover teacher must belong to the lesson's branch."""
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
    foreign_user = user_in(tenant_a, roles=[Role.TEACHER], branch=other_branch)
    with schema_context(tenant_a.schema_name):
        foreign_prof = TeacherProfileFactory.create(user=foreign_user, branch=other_branch)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    r = s["manager"].post(f"{COVER}{cid}/assign/", {"cover_teacher": foreign_prof.id}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "cover_teacher_out_of_branch"
    # the lesson is untouched and the request stays open
    with schema_context(tenant_a.schema_name):
        s["lesson"].refresh_from_db()
        assert s["lesson"].teacher_id == s["a_prof"].id
        from apps.covers.models import CoverRequest

        assert CoverRequest.objects.get(pk=cid).status == "open"


def test_recover_chain_after_approval(tenant_a, user_in, as_user):
    """Once a cover is approved the lesson belongs to the cover teacher; if THEY then
    need cover, a fresh request must be allowed (the approved row is historical)."""
    from apps.teachers.tests.factories import TeacherProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    # A -> B (B now teaches the lesson)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    assert (
        s["manager"]
        .post(f"{COVER}{cid}/assign/", {"cover_teacher": s["b_prof"].id}, format="json")
        .json()["data"]["status"]
        == "approved"
    )
    # B now requests cover for the same (reassigned) lesson — must not be blocked
    again = s["b_client"].post(COVER, {"lesson": s["lesson"].id}, format="json")
    assert again.status_code == 201, again.content
    cid2 = again.json()["data"]["id"]
    # ...and it can be assigned onward to a third teacher C
    c_user = user_in(tenant_a, roles=[Role.TEACHER], branch=s["branch"])
    with schema_context(tenant_a.schema_name):
        c_prof = TeacherProfileFactory.create(user=c_user, branch=s["branch"])
    s["manager"].post(f"{COVER}{cid2}/assign/", {"cover_teacher": c_prof.id}, format="json")
    with schema_context(tenant_a.schema_name):
        s["lesson"].refresh_from_db()
        assert s["lesson"].teacher_id == c_prof.id


def test_manager_cannot_cancel_anothers_request(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    r = s["manager"].post(f"{COVER}{cid}/cancel/", {}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "not_requester"
    with schema_context(tenant_a.schema_name):
        from apps.covers.models import CoverRequest

        assert CoverRequest.objects.get(pk=cid).status == "open"


def test_non_teacher_cannot_claim(tenant_a, user_in, as_user):
    """A cover:write holder with no TeacherProfile (a manager) cannot claim."""
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    s["manager"].post(f"{COVER}{cid}/open-pool/", {}, format="json")
    r = s["manager"].post(f"{COVER}{cid}/claim/", {}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "not_a_teacher"


def test_cannot_claim_non_pooled_request(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    # the requester (who can see their own request) tries to claim it before it was
    # ever opened to the pool — the pool guard rejects it (no out-of-pool claim)
    r = s["a_client"].post(f"{COVER}{cid}/claim/", {}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "cover_not_claimable"


def test_reject_frees_the_constraint(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    assert (
        s["manager"].post(f"{COVER}{cid}/reject/", {}, format="json").json()["data"]["status"] == "rejected"
    )
    # a rejection frees the lesson for a fresh request
    again = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json")
    assert again.status_code == 201


def _ids(response):
    body = response.json()
    items = body["data"] if isinstance(body, dict) and "data" in body else body
    return [row["id"] for row in items]


def test_opening_to_pool_notifies_the_claimable_teacher_pool(tenant_a, user_in, as_user):
    """F18-2: opening a cover to the pool pushes a realtime notification to the branch's
    claimable teachers (cover:write + a teacher profile), so they learn a lesson is up for
    grabs without polling — but NOT the requester being covered nor the manager who opened
    it, and the payload carries the cover id so a client can deep-link straight to claim."""
    from apps.notifications.models import Notification

    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    s["manager"].post(f"{COVER}{cid}/open-pool/", {}, format="json")

    with schema_context(tenant_a.schema_name):
        rows = Notification.objects.filter(event_type="cover.pool_opened")
        recipients = set(rows.values_list("user_id", flat=True))
        assert s["b_prof"].user_id in recipients  # teacher B can claim -> notified
        assert s["a_prof"].user_id not in recipients  # the requester is being covered
        assert rows.get(user_id=s["b_prof"].user_id).data["cover_id"] == cid


def test_pool_board_lists_only_pooled_open_covers(tenant_a, user_in, as_user):
    """F18-2: the /cover/pool/ board shows a teacher the requests opened to the pool in
    their branch — empty until a manager opens one, then listing it for the taking."""
    s = _setup(tenant_a, user_in, as_user)
    cid = s["a_client"].post(COVER, {"lesson": s["lesson"].id}, format="json").json()["data"]["id"]
    before = s["b_client"].get(f"{COVER}pool/")
    assert before.status_code == 200, before.content
    assert _ids(before) == []  # not on the board until a manager opens it
    s["manager"].post(f"{COVER}{cid}/open-pool/", {}, format="json")
    assert cid in _ids(s["b_client"].get(f"{COVER}pool/"))


def test_role_without_cover_is_denied(tenant_a, as_role):
    cashier_client, _ = as_role(Role.CASHIER)  # cashier holds no cover permission
    assert cashier_client.get(COVER).status_code == 403
