"""F5 — tasks + role hierarchy: create/assign with hierarchy gating, status
lifecycle, scoping (assignee / department / manager), and grade management."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TASKS = "/api/v1/tasks/"
GRADES = "/api/v1/tasks/grades/"


def _rows(body):
    return body["data"] if isinstance(body, dict) and "data" in body else body


def test_hierarchy_gated_assignment(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    director.post(GRADES, {"role": "teacher", "level": 2}, format="json")
    director.post(GRADES, {"role": "registrar", "level": 1}, format="json")

    teacher_client, teacher = as_role(Role.TEACHER)
    registrar_client, registrar = as_role(Role.REGISTRAR)

    # teacher (grade 2) may task the registrar (grade 1)
    ok = teacher_client.post(TASKS, {"title": "file these", "assignee": registrar.id}, format="json")
    assert ok.status_code == 201, ok.content

    # registrar (grade 1) may NOT task the teacher (grade 2)
    blocked = registrar_client.post(TASKS, {"title": "grade these", "assignee": teacher.id}, format="json")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "cannot_assign_grade"


def test_director_bypasses_hierarchy(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    _tc, teacher = as_role(Role.TEACHER)
    director.post(GRADES, {"role": "teacher", "level": 9}, format="json")
    # director holds tasks:assign_any (via *:*) -> can task even a top-grade role
    r = director.post(TASKS, {"title": "x", "assignee": teacher.id}, format="json")
    assert r.status_code == 201, r.content


def test_task_status_lifecycle(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    tid = director.post(TASKS, {"title": "x"}, format="json").json()["data"]["id"]

    def transition(s):
        return director.post(f"{TASKS}{tid}/transition/", {"status": s}, format="json")

    assert transition("in_progress").json()["data"]["status"] == "in_progress"
    done = transition("done")
    assert done.json()["data"]["status"] == "done"
    assert done.json()["data"]["completed_at"] is not None

    bad = transition("in_progress")  # done -> in_progress is not allowed
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_transition"

    reopened = transition("open")  # done -> open (reopen) clears completion
    assert reopened.json()["data"]["status"] == "open"
    assert reopened.json()["data"]["completed_at"] is None


def test_assignee_sees_and_transitions_own_task(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    worker_client, worker = as_role(Role.SUPPORT)  # tasks:read only
    director.post(TASKS, {"title": "for worker", "assignee": worker.id}, format="json")

    rows = _rows(worker_client.get(f"{TASKS}mine/").json())
    assert len(rows) == 1
    assert rows[0]["title"] == "for worker"
    tid = rows[0]["id"]

    # the assignee can transition their own task...
    assert (
        worker_client.post(f"{TASKS}{tid}/transition/", {"status": "in_progress"}, format="json").status_code
        == 200
    )
    # ...but cannot create tasks (no tasks:write)
    assert worker_client.post(TASKS, {"title": "x"}, format="json").status_code == 403


def test_unassigned_user_does_not_see_others_tasks(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    _wc, worker = as_role(Role.SUPPORT)
    other_client, _other = as_role(Role.SUPPORT)
    director.post(TASKS, {"title": "for worker", "assignee": worker.id}, format="json")
    # the other support user is neither assignee, creator, nor in the dept/branch
    assert _rows(other_client.get(f"{TASKS}mine/").json()) == []
    assert _rows(other_client.get(TASKS).json()) == []


def test_department_assignment_is_visible_to_members(tenant_a, as_role, user_in, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        dept = DepartmentFactory.create(branch=branch)
    member = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    with schema_context(tenant_a.schema_name):
        from apps.users.models import RoleMembership

        RoleMembership.objects.filter(user=member, branch=branch).update(department=dept)
    member_client = as_user(tenant_a, member)

    director.post(TASKS, {"title": "dept work", "department": dept.id}, format="json")
    rows = _rows(member_client.get(TASKS).json())
    assert any(r["title"] == "dept work" for r in rows)


def test_only_senior_can_edit_hierarchy(tenant_a, as_role):
    teacher_client, _ = as_role(Role.TEACHER)  # tasks:write but not tasks:assign_any
    assert teacher_client.post(GRADES, {"role": "teacher", "level": 1}, format="json").status_code == 403
    assert teacher_client.get(GRADES).status_code == 200  # but may read the hierarchy

    director, _ = as_role(Role.DIRECTOR)
    assert director.post(GRADES, {"role": "teacher", "level": 1}, format="json").status_code == 201
    # role is unique -> a duplicate grade is a clean 400, not a 500
    assert director.post(GRADES, {"role": "teacher", "level": 2}, format="json").status_code == 400


def test_grade_list_orders_by_level_then_role(tenant_a, as_role):
    """The hierarchy list keeps the model's ("-level", "role") order — equal-level
    grades fall back to a deterministic role tiebreak (not DB-arbitrary)."""
    director, _ = as_role(Role.DIRECTOR)
    director.post(GRADES, {"role": "teacher", "level": 1}, format="json")
    director.post(GRADES, {"role": "registrar", "level": 1}, format="json")
    director.post(GRADES, {"role": "head_of_dept", "level": 5}, format="json")
    rows = _rows(director.get(GRADES).json())
    order = [(r["level"], r["role"]) for r in rows]
    # level desc, then role asc among equals (registrar before teacher)
    assert order == [(5, "head_of_dept"), (1, "registrar"), (1, "teacher")]


def test_students_have_no_task_access(tenant_a, as_role):
    student, _ = as_role(Role.STUDENT)
    assert student.get(TASKS).status_code == 403
    assert student.post(TASKS, {"title": "x"}, format="json").status_code == 403


# --------------------------------------------------------------------------- #
# review hardening
# --------------------------------------------------------------------------- #
def test_reassign_is_hierarchy_gated(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    director.post(GRADES, {"role": "teacher", "level": 2}, format="json")
    director.post(GRADES, {"role": "registrar", "level": 1}, format="json")
    registrar_client, _r = as_role(Role.REGISTRAR)
    _tc, teacher = as_role(Role.TEACHER)
    tid = registrar_client.post(TASKS, {"title": "x"}, format="json").json()["data"]["id"]
    # the gate applies on reassign too, not just create
    up = registrar_client.post(f"{TASKS}{tid}/assign/", {"assignee": teacher.id}, format="json")
    assert up.status_code == 403
    assert up.json()["code"] == "cannot_assign_grade"


def test_ungraded_target_fails_closed_when_hierarchy_configured(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    director.post(GRADES, {"role": "teacher", "level": 2}, format="json")  # hierarchy now in use
    teacher_client, _t = as_role(Role.TEACHER)
    _sc, support = as_role(Role.SUPPORT)  # SUPPORT is ungraded
    # a graded teacher may not task an UNPLACED role (can't exploit a forgotten grade)
    blocked = teacher_client.post(TASKS, {"title": "x", "assignee": support.id}, format="json")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "cannot_assign_grade"
    # the director (assign_any) still can
    assert director.post(TASKS, {"title": "y", "assignee": support.id}, format="json").status_code == 201


def test_cross_branch_task_creation_blocked(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
        dept_b = DepartmentFactory.create(branch=branch_b)
    teacher_a = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a))

    cross = teacher_a.post(TASKS, {"title": "x", "branch": branch_b.id}, format="json")
    assert cross.status_code == 403
    assert cross.json()["code"] == "cross_branch"

    cross_dept = teacher_a.post(TASKS, {"title": "x", "department": dept_b.id}, format="json")
    assert cross_dept.status_code == 403
    assert cross_dept.json()["code"] == "cross_branch_dept"


def test_cannot_assign_a_non_staff_user(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    _sc, student = as_role(Role.STUDENT)
    # a student is not staff -> not a valid assignee
    r = director.post(TASKS, {"title": "x", "assignee": student.id}, format="json")
    assert r.status_code == 400


def test_transition_of_unseen_task_is_404(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    tid = director.post(TASKS, {"title": "secret"}, format="json").json()["data"]["id"]
    worker_client, _w = as_role(Role.SUPPORT)  # not assignee/creator/dept/branch
    r = worker_client.post(f"{TASKS}{tid}/transition/", {"status": "in_progress"}, format="json")
    assert r.status_code == 404


def test_done_can_be_cancelled_and_same_status_is_noop(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    tid = director.post(TASKS, {"title": "x"}, format="json").json()["data"]["id"]
    director.post(f"{TASKS}{tid}/transition/", {"status": "done"}, format="json")
    cancelled = director.post(f"{TASKS}{tid}/transition/", {"status": "cancelled"}, format="json")
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    # repeating the same status is a no-op, not a 422
    noop = director.post(f"{TASKS}{tid}/transition/", {"status": "cancelled"}, format="json")
    assert noop.status_code == 200


def test_manager_cannot_see_other_branch_tasks(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
    mgr_a = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a))
    mgr_b = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch_b))
    tid = mgr_b.post(TASKS, {"title": "b task"}, format="json").json()["data"]["id"]
    # branch-A manager neither sees nor can fetch branch-B's task
    assert mgr_a.get(f"{TASKS}{tid}/").status_code == 404
    assert _rows(mgr_a.get(TASKS).json()) == []
