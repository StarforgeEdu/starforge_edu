"""F24-1 (staff) — disciplinary penalties against a STAFF member, on the same ledger as
student demerits. Manager-gated (penalty:staff — not a peer teacher), self-penalty
blocked, subject must be staff. HR-PRIVACY: a staff disciplinary record is visible to
managers + the subject, but NEVER to peer teachers."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

PEN = "/api/v1/rulebook/penalties/"
STAFF = PEN + "staff/"


def _setup(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    hod_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)  # manager: penalty:staff
    teacher_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)  # penalty:write only (a peer)
    target_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)  # the staff member disciplined
    return {
        "branch": branch,
        "hod_u": hod_u,
        "teacher_u": teacher_u,
        "target_u": target_u,
        "manager": as_user(tenant, hod_u),
        "teacher": as_user(tenant, teacher_u),
        "target": as_user(tenant, target_u),
    }


def _discipline(s, **over):
    payload = {"staff": s["target_u"].id, "branch": s["branch"].id, "points": 3, "reason": "late to class"}
    payload.update(over)
    return s["manager"].post(STAFF, payload, format="json")


def test_manager_disciplines_a_staff_member(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _discipline(s)
    assert r.status_code == 201, r.content
    body = r.json()["data"]
    assert body["staff"] == s["target_u"].id
    assert body["student"] is None
    assert body["points"] == 3
    assert body["status"] == "active"


def test_a_peer_teacher_cannot_discipline_staff(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = s["teacher"].post(
        STAFF,
        {"staff": s["target_u"].id, "branch": s["branch"].id, "points": 2, "reason": "x"},
        format="json",
    )
    assert r.status_code == 403  # penalty:write does not grant penalty:staff


def test_cannot_penalise_yourself(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _discipline(s, staff=s["hod_u"].id)
    assert r.status_code == 422
    assert r.json()["code"] == "self_penalty"


def test_subject_must_be_an_active_staff_member(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    student_u = user_in(tenant_a, roles=[Role.STUDENT], branch=s["branch"])  # not staff
    r = _discipline(s, staff=student_u.id)
    assert r.status_code == 422
    assert r.json()["code"] == "not_staff"


def test_branch_scope_is_enforced(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other = BranchFactory.create()
    r = _discipline(s, branch=other.id)  # HOD's branch is `branch`, not `other`
    assert r.status_code == 403
    assert r.json()["code"] == "branch_out_of_scope"


def test_peer_teacher_cannot_see_a_staff_penalty(tenant_a, user_in, as_user):
    """HR privacy: a teacher (penalty:write) sees their branch's STUDENT demerits but
    NOT a colleague's disciplinary record."""
    s = _setup(tenant_a, user_in, as_user)
    pid = _discipline(s).json()["data"]["id"]
    listed = s["teacher"].get(PEN).json()["data"]
    assert pid not in [p["id"] for p in listed]


def test_the_subject_sees_their_own_staff_penalty(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    pid = _discipline(s).json()["data"]["id"]
    listed = s["target"].get(PEN).json()["data"]  # target_u is the subject
    assert pid in [p["id"] for p in listed]


def test_a_manager_sees_branch_staff_penalties(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    pid = _discipline(s).json()["data"]["id"]
    listed = s["manager"].get(PEN).json()["data"]
    assert pid in [p["id"] for p in listed]


def test_a_staff_penalty_can_be_waived(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    pid = _discipline(s).json()["data"]["id"]
    w = s["manager"].post(f"{PEN}{pid}/waive/", {"reason": "appeal upheld"}, format="json")
    assert w.status_code == 200
    assert w.json()["data"]["status"] == "waived"
    assert w.json()["data"]["waive_reason"] == "appeal upheld"


def test_a_non_teacher_staff_subject_can_see_their_own_record(tenant_a, user_in, as_user):
    """Every disciplinable staff role (not just teachers) holds penalty:read, so the
    subject can always read the record filed against them (transparency invariant)."""
    s = _setup(tenant_a, user_in, as_user)
    accountant_u = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=s["branch"])
    pid = _discipline(s, staff=accountant_u.id).json()["data"]["id"]
    listed = as_user(tenant_a, accountant_u).get(PEN).json()["data"]
    assert pid in [p["id"] for p in listed]


def test_cannot_discipline_a_staff_member_from_another_branch(tenant_a, user_in, as_user):
    """The subject must work in the penalty's branch — symmetric with the student path —
    so a manager can't file discipline against staff outside their branch."""
    from apps.org.tests.factories import BranchFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other = BranchFactory.create()
    outsider = user_in(tenant_a, roles=[Role.TEACHER], branch=other)  # works only in `other`
    r = _discipline(s, staff=outsider.id)  # filed under s["branch"], where they don't work
    assert r.status_code == 422
    assert r.json()["code"] == "not_staff"


def test_staff_penalty_does_not_escalate(tenant_a, user_in, as_user):
    """The point-threshold escalation is a student-intake signal; staff discipline never
    sets the escalated flag (no _active_points lookup over a staff member)."""
    from apps.org.models import CenterSettings

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        cs = CenterSettings.load()
        cs.penalty_escalation_threshold = 1  # would escalate any student penalty
        cs.save()
    body = _discipline(s, points=99).json()["data"]
    assert body["escalated"] is False
