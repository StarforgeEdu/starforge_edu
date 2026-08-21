"""F24-1 — student demerits tied to the rule book: a teacher/manager issues a penalty,
a manager (separate perm) waives it; the student/guardians read their own record."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

PEN = "/api/v1/rulebook/penalties/"


def _setup(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        student = StudentProfileFactory.create(branch=branch)
    return {
        "branch": branch,
        "student": student,
        "teacher": as_user(tenant, user_in(tenant, roles=[Role.TEACHER], branch=branch)),
        "manager": as_user(tenant, user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)),
    }


def test_issue_then_waive_penalty(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    issued = s["teacher"].post(
        PEN, {"student": s["student"].id, "points": 5, "reason": "repeatedly late"}, format="json"
    )
    assert issued.status_code == 201, issued.content
    pid = issued.json()["data"]["id"]
    assert issued.json()["data"]["status"] == "active"
    assert issued.json()["data"]["points"] == 5

    # the teacher who issued holds penalty:write but NOT penalty:waive (SoD)
    assert s["teacher"].post(f"{PEN}{pid}/waive/", {}, format="json").status_code == 403
    # a manager waives it (with a reason)
    waived = s["manager"].post(f"{PEN}{pid}/waive/", {"reason": "first offence"}, format="json")
    assert waived.status_code == 200
    assert waived.json()["data"]["status"] == "waived"
    assert waived.json()["data"]["waive_reason"] == "first offence"
    # a waived penalty can't be waived again
    assert s["manager"].post(f"{PEN}{pid}/waive/", {}, format="json").status_code == 422


def test_cannot_penalise_another_branchs_student(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        other_student = StudentProfileFactory.create(branch=other_branch)
    r = s["teacher"].post(PEN, {"student": other_student.id, "points": 3, "reason": "x"}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "branch_out_of_scope"


def test_student_sees_only_own_penalties(tenant_a, user_in, as_user):
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    branch = s["branch"]
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        subject = StudentProfileFactory.create(user=student_user, branch=branch)
    s["teacher"].post(PEN, {"student": subject.id, "points": 2, "reason": "noise"}, format="json")
    s["teacher"].post(PEN, {"student": s["student"].id, "points": 9, "reason": "other kid"}, format="json")

    body = as_user(tenant_a, student_user).get(PEN).json()
    assert body["pagination"]["total"] == 1  # only their own demerit, not the other student's
    assert body["data"][0]["student"] == subject.id


def test_guardian_sees_only_their_childs_penalties(tenant_a, user_in, as_user):
    from apps.parents.models import ParentProfile
    from apps.parents.tests.factories import GuardianFactory
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    parent_user = user_in(tenant_a, roles=[Role.PARENT], branch=s["branch"])
    with schema_context(tenant_a.schema_name):
        parent_profile = ParentProfile.objects.create(user=parent_user)
        GuardianFactory.create(parent=parent_profile, student=s["student"], is_primary=True)
        other_child = StudentProfileFactory.create(branch=s["branch"])  # not this parent's
    s["teacher"].post(PEN, {"student": s["student"].id, "points": 4, "reason": "uniform"}, format="json")
    s["teacher"].post(PEN, {"student": other_child.id, "points": 7, "reason": "someone else"}, format="json")

    body = as_user(tenant_a, parent_user).get(PEN).json()
    assert body["pagination"]["total"] == 1  # ONLY their child's record, not the other student's
    assert body["data"][0]["student"] == s["student"].id


def test_staff_list_is_branch_scoped(tenant_a, user_in, as_user):
    from apps.compliance.models import Penalty
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    # a penalty in the teacher's OWN branch...
    s["teacher"].post(PEN, {"student": s["student"].id, "points": 2, "reason": "mine"}, format="json")
    # ...and one in ANOTHER branch (created directly)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        other_student = StudentProfileFactory.create(branch=other_branch)
        Penalty.objects.create(student=other_student, points=9, reason="elsewhere", branch=other_branch)

    body = s["teacher"].get(PEN).json()
    assert body["pagination"]["total"] == 1  # the teacher sees only their own branch's penalties
    assert body["data"][0]["branch"] == s["branch"].id


def test_penalty_can_cite_an_active_rule_only(tenant_a, user_in, as_user):
    from apps.compliance.models import Rule

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        active = Rule.objects.create(title="No phones in class", body="...", is_active=True)
        inactive = Rule.objects.create(title="Retired rule", body="...", is_active=False)

    cited = s["teacher"].post(
        PEN, {"student": s["student"].id, "points": 2, "reason": "phone", "rule": active.id}, format="json"
    )
    assert cited.status_code == 201
    assert cited.json()["data"]["rule"] == active.id  # the breached rule is linked
    bad = s["teacher"].post(
        PEN, {"student": s["student"].id, "points": 2, "reason": "x", "rule": inactive.id}, format="json"
    )
    assert bad.status_code == 400  # a retired rule can't be cited


def test_student_cannot_issue_a_penalty(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=s["branch"])
    client = as_user(tenant_a, student_user)
    # students hold penalty:read (see own) but never penalty:write
    r = client.post(PEN, {"student": s["student"].id, "points": 1, "reason": "x"}, format="json")
    assert r.status_code == 403


def test_a_read_only_role_cannot_issue_a_penalty(tenant_a, as_role, user_in, as_user):
    """F24-1: every staff role now holds penalty:read so they can see their OWN
    disciplinary record — a cashier may LIST (scoped to their own, empty here) — but
    issuing a penalty still needs penalty:write, which they do NOT hold (SoD)."""
    s = _setup(tenant_a, user_in, as_user)
    cashier, _ = as_role(Role.CASHIER)  # penalty:read (own record) but no penalty:write
    assert cashier.get(PEN).status_code == 200  # reads their own (none) — not a leak
    assert cashier.get(PEN).json()["data"] == []
    assert (
        cashier.post(PEN, {"student": s["student"].id, "points": 1, "reason": "x"}, format="json").status_code
        == 403
    )
