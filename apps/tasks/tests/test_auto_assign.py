"""F5-4 — fair task auto-split: distribute a department's open tasks across its staff
balanced by current open-task load (least-loaded first), or leave them claimable.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TASKS = "/api/v1/tasks/"
AUTO = "/api/v1/tasks/auto-assign/"


def _dept(tenant, branch):
    from apps.org.tests.factories import DepartmentFactory

    with schema_context(tenant.schema_name):
        return DepartmentFactory.create(branch=branch)


def _staff_in(tenant, user_in, branch, dept, *, role=Role.SUPPORT):
    from apps.users.models import RoleMembership

    user = user_in(tenant, roles=[role], branch=branch)
    with schema_context(tenant.schema_name):
        RoleMembership.objects.filter(user=user, branch=branch).update(department=dept)
    return user


def _open_tasks(director, dept, n, **extra):
    return [
        director.post(TASKS, {"title": f"t{i}", "department": dept.id, **extra}, format="json").json()[
            "data"
        ]["id"]
        for i in range(n)
    ]


def test_fair_split_balances_across_staff(tenant_a, as_role, user_in):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    dept = _dept(tenant_a, branch)
    s1 = _staff_in(tenant_a, user_in, branch, dept)
    s2 = _staff_in(tenant_a, user_in, branch, dept)
    task_ids = _open_tasks(director, dept, 4)

    r = director.post(AUTO, {"task_ids": task_ids, "department": dept.id, "mode": "fair"}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()["data"]
    assert body["assigned"] == 4
    counts: dict[int, int] = {}
    for a in body["assignments"]:
        counts[a["assignee"]] = counts.get(a["assignee"], 0) + 1
    assert sorted(counts.values()) == [2, 2]  # evenly split
    assert set(counts) == {s1.id, s2.id}


def test_fair_split_respects_existing_load(tenant_a, as_role, user_in):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    dept = _dept(tenant_a, branch)
    busy = _staff_in(tenant_a, user_in, branch, dept)
    free = _staff_in(tenant_a, user_in, branch, dept)
    # `busy` already carries 3 open tasks
    _open_tasks(director, dept, 3, assignee=busy.id)
    new_ids = _open_tasks(director, dept, 2)

    body = director.post(
        AUTO, {"task_ids": new_ids, "department": dept.id, "mode": "fair"}, format="json"
    ).json()["data"]
    # both new tasks go to the least-loaded `free` (0 < 3)
    assert {a["assignee"] for a in body["assignments"]} == {free.id}


def test_fair_split_rebalances_an_overloaded_persons_pile(tenant_a, as_role, user_in):
    """Redistributing A's own pile must SPREAD it, not count it as A's fixed load and
    dump everything on idle B (the inverse of balancing)."""
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    dept = _dept(tenant_a, branch)
    a = _staff_in(tenant_a, user_in, branch, dept)
    b = _staff_in(tenant_a, user_in, branch, dept)
    task_ids = _open_tasks(director, dept, 3, assignee=a.id)  # A is overloaded; B idle

    body = director.post(
        AUTO, {"task_ids": task_ids, "department": dept.id, "mode": "fair"}, format="json"
    ).json()["data"]
    counts: dict[int, int] = {}
    for x in body["assignments"]:
        counts[x["assignee"]] = counts.get(x["assignee"], 0) + 1
    assert body["assigned"] == 3
    assert set(counts) == {a.id, b.id}  # both share — A is NOT emptied onto B
    assert max(counts.values()) == 2  # 3 across 2 -> 2/1, not 3/0


def test_fair_split_never_assigns_to_a_student_member(tenant_a, as_role, user_in):
    """Only taskable staff are eligible — a student/parent with a dept membership (who
    could never see the task) is excluded, matching the manual assign path."""
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    dept = _dept(tenant_a, branch)
    staff = _staff_in(tenant_a, user_in, branch, dept)
    _staff_in(tenant_a, user_in, branch, dept, role=Role.STUDENT)  # not taskable
    task_ids = _open_tasks(director, dept, 2)

    body = director.post(
        AUTO, {"task_ids": task_ids, "department": dept.id, "mode": "fair"}, format="json"
    ).json()["data"]
    assert {x["assignee"] for x in body["assignments"]} == {staff.id}  # all to staff, never the student


def test_free_mode_unassigns(tenant_a, as_role, user_in):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    dept = _dept(tenant_a, branch)
    s1 = _staff_in(tenant_a, user_in, branch, dept)
    (tid,) = _open_tasks(director, dept, 1, assignee=s1.id)

    body = director.post(
        AUTO, {"task_ids": [tid], "department": dept.id, "mode": "free"}, format="json"
    ).json()["data"]
    assert body["freed"] == 1
    with schema_context(tenant_a.schema_name):
        from apps.tasks.models import Task

        assert Task.objects.get(pk=tid).assignee_id is None  # now claimable


def test_no_open_tasks_in_department(tenant_a, as_role, user_in):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    dept = _dept(tenant_a, branch)
    _staff_in(tenant_a, user_in, branch, dept)
    r = director.post(AUTO, {"task_ids": [999999], "department": dept.id}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "no_open_tasks"


def test_no_eligible_staff_in_empty_department(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    dept = _dept(tenant_a, branch)  # no members
    task_ids = _open_tasks(director, dept, 1)
    r = director.post(AUTO, {"task_ids": task_ids, "department": dept.id, "mode": "fair"}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "no_eligible_staff"


def test_hierarchy_gate_excludes_higher_grade_staff(tenant_a, as_role, user_in, as_user):
    """A non-director may only auto-assign to equal/lower grades — a department whose
    only member outranks the actor yields no eligible staff."""
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    director.post("/api/v1/tasks/grades/", {"role": "teacher", "level": 2}, format="json")
    director.post("/api/v1/tasks/grades/", {"role": "head_of_dept", "level": 5}, format="json")
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    dept = _dept(tenant_a, branch)
    # the department's only member is an HOD (grade 5), above the teacher (grade 2)
    _staff_in(tenant_a, user_in, branch, dept, role=Role.HEAD_OF_DEPT)
    teacher_u = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    teacher = as_user(tenant_a, teacher_u)
    task_ids = _open_tasks(director, dept, 1)
    r = teacher.post(AUTO, {"task_ids": task_ids, "department": dept.id, "mode": "fair"}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "no_eligible_staff"


def test_student_cannot_auto_assign(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    dept = _dept(tenant_a, branch)
    task_ids = _open_tasks(director, dept, 1)
    student, _ = as_role(Role.STUDENT)  # no tasks:write
    assert student.post(AUTO, {"task_ids": task_ids, "department": dept.id}, format="json").status_code == 403
