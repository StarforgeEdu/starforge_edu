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


def test_hierarchy_gated_assignment(tenant_a, as_role, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    director.post(GRADES, {"role": "teacher", "level": 2}, format="json")
    director.post(GRADES, {"role": "registrar", "level": 1}, format="json")

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    teacher = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    registrar = user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch)
    teacher_client = as_user(tenant_a, teacher)
    registrar_client = as_user(tenant_a, registrar)

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
def test_reassign_is_hierarchy_gated(tenant_a, as_role, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    director.post(GRADES, {"role": "teacher", "level": 2}, format="json")
    director.post(GRADES, {"role": "registrar", "level": 1}, format="json")
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    registrar = user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch)
    teacher = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    registrar_client = as_user(tenant_a, registrar)
    tid = registrar_client.post(TASKS, {"title": "x"}, format="json").json()["data"]["id"]
    # the gate applies on reassign too, not just create
    up = registrar_client.post(f"{TASKS}{tid}/assign/", {"assignee": teacher.id}, format="json")
    assert up.status_code == 403
    assert up.json()["code"] == "cannot_assign_grade"


def test_ungraded_target_fails_closed_when_hierarchy_configured(tenant_a, as_role, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    director.post(GRADES, {"role": "teacher", "level": 2}, format="json")  # hierarchy now in use
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    teacher = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    support = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)  # SUPPORT is ungraded
    teacher_client = as_user(tenant_a, teacher)
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
    assert cross.json()["code"] == "out_of_scope"

    cross_dept = teacher_a.post(TASKS, {"title": "x", "department": dept_b.id}, format="json")
    assert cross_dept.status_code == 403
    assert cross_dept.json()["code"] == "out_of_scope"


def test_cannot_assign_a_non_staff_user(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    _sc, student = as_role(Role.STUDENT)
    # a student is not staff -> not a valid assignee
    r = director.post(TASKS, {"title": "x", "assignee": student.id}, format="json")
    assert r.status_code == 400


def test_shared_bridge_role_cannot_read_transition_or_be_selected_for_another_principal(
    tenant_a, as_role, client_for, django_assert_num_queries
):
    from apps.org.tests.factories import BranchFactory
    from apps.tasks.models import Task
    from tests.role_principal_helpers import exact_session_client, shared_staff_teacher_bridge

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        user, teacher, staff = shared_staff_teacher_bridge(
            branch=branch,
            staff_role=Role.SUPPORT,
        )
        teacher_id = teacher.pk
        task = Task.objects.create(
            title="Teacher account only",
            assignee=user,
            assignee_principal_kind="teacher",
            assignee_principal_id=teacher.pk,
        )

    teacher_client = exact_session_client(
        client_for,
        tenant_a,
        user,
        principal_kind="teacher",
        principal_id=teacher_id,
    )
    staff_client = exact_session_client(
        client_for,
        tenant_a,
        user,
        principal_kind="staff",
        principal_id=staff.pk,
    )
    assert [row["id"] for row in _rows(teacher_client.get(f"{TASKS}mine/").json())] == [task.pk]
    assert _rows(staff_client.get(f"{TASKS}mine/").json()) == []
    assert staff_client.get(f"{TASKS}{task.pk}/").status_code == 404
    assert (
        staff_client.post(f"{TASKS}{task.pk}/transition/", {"status": "done"}, format="json").status_code
        == 404
    )
    assert (
        teacher_client.post(f"{TASKS}{task.pk}/transition/", {"status": "done"}, format="json").status_code
        == 200
    )

    # The compatibility user id is not an acceptable write selector when two
    # active staff principals sit behind it.
    ambiguous = director.post(
        TASKS,
        {"title": "Unsafe assignment", "branch": branch.pk, "assignee": user.pk},
        format="json",
    )
    assert ambiguous.status_code == 400
    assert ambiguous.json()["errors"] == {"assignee": ["Choose active task staff in the task's scope."]}

    selected = director.post(
        TASKS,
        {
            "title": "Exact teacher assignment",
            "branch": branch.pk,
            "assignee_principal": {"kind": "teacher", "id": teacher.pk},
        },
        format="json",
    )
    assert selected.status_code == 201, selected.content
    payload = selected.json()["data"]
    assert payload["assignee"] == user.pk  # deprecated compatibility bridge
    assert payload["assignee_principal"]["kind"] == "teacher"
    assert payload["assignee_principal"]["id"] == teacher.pk
    assert payload["assignee_name"]
    assert payload["branch_name"] == branch.name
    exact_filter = director.get(f"{TASKS}?assignee_kind=teacher&assignee_principal_id={teacher.pk}")
    assert {row["id"] for row in _rows(exact_filter.json())} == {task.pk, payload["id"]}

    from apps.tasks.presenters import task_to_dict
    from apps.tasks.repositories.task_repository import TaskRepository

    with schema_context(tenant_a.schema_name), django_assert_num_queries(1):
        loaded = TaskRepository().get_queryset().get(pk=payload["id"])
        bounded_payload = task_to_dict(loaded)
    assert bounded_payload["assignee_name"]


def test_assigned_task_protects_bridge_identity_lifecycle(tenant_a, as_role):
    from django.db.models import ProtectedError

    from apps.users.models import User

    director, _ = as_role(Role.DIRECTOR)
    _worker_client, worker = as_role(Role.SUPPORT)
    created = director.post(
        TASKS,
        {"title": "Historical owner", "assignee": worker.pk},
        format="json",
    )
    assert created.status_code == 201, created.content
    with schema_context(tenant_a.schema_name), pytest.raises(ProtectedError):
        User.objects.get(pk=worker.pk).delete()


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


def test_role_grade_detail_full_update_and_delete_are_wired(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    created = director.post(GRADES, {"role": "teacher", "level": 2, "label": "Teacher"}, format="json")
    grade_id = created.json()["data"]["id"]
    detail = f"{GRADES}{grade_id}/"

    assert director.get(detail).json()["data"]["level"] == 2
    assert director.head(detail).status_code == 200
    missing = director.put(detail, {"level": 3}, format="json")
    assert missing.status_code == 400
    assert "role" in missing.json()["errors"]
    assert director.patch(detail, {"label": "Senior teacher"}, format="json").status_code == 200
    replaced = director.put(detail, {"role": "teacher", "level": 4}, format="json")
    assert replaced.status_code == 200
    assert replaced.json()["data"]["level"] == 4
    assert director.delete(detail).status_code == 204
    assert director.get(detail).status_code == 404


def test_assign_action_and_detail_happy_path(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    _worker_client, worker = as_role(Role.SUPPORT)
    task_id = director.post(TASKS, {"title": "Assign later"}, format="json").json()["data"]["id"]
    assigned = director.post(f"{TASKS}{task_id}/assign/", {"assignee": worker.id}, format="json")
    assert assigned.status_code == 200, assigned.content
    assert assigned.json()["data"]["assignee"] == worker.id
    assert director.get(f"{TASKS}{task_id}/").json()["data"]["title"] == "Assign later"


def test_department_peer_cannot_transition_someone_elses_task(tenant_a, user_in, as_user, as_role):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.users.models import RoleMembership

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        department = DepartmentFactory.create(branch=branch)
    assignee = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    peer = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.filter(user_id__in=(assignee.id, peer.id), branch=branch).update(
            department=department
        )
    task_id = director.post(
        TASKS,
        {"title": "Private ownership", "department": department.id, "assignee": assignee.id},
        format="json",
    ).json()["data"]["id"]

    peer_client = as_user(tenant_a, peer)
    assert peer_client.get(f"{TASKS}{task_id}/").status_code == 200  # visible through department
    denied = peer_client.post(f"{TASKS}{task_id}/transition/", {"status": "done"}, format="json")
    assert denied.status_code == 403
    assert denied.json()["code"] == "not_task_assignee"
    assert (
        as_user(tenant_a, assignee)
        .post(f"{TASKS}{task_id}/transition/", {"status": "done"}, format="json")
        .status_code
        == 200
    )


def test_single_archived_branch_is_not_auto_selected(tenant_a, user_in, as_user):
    from django.utils import timezone

    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        archived = BranchFactory.create(archived_at=timezone.now())
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=archived))
    response = client.post(TASKS, {"title": "Do not pin me"}, format="json")
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"


def test_task_list_routes_support_head(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    assert director.head(TASKS).status_code == 200
    assert director.head(f"{TASKS}mine/").status_code == 200
    assert director.head(GRADES).status_code == 200


def test_task_unknown_fields_filters_and_resource_bounds_are_rejected(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    assert director.post(TASKS, {"title": "x", "surprise": True}, format="json").status_code == 400
    assert director.get(f"{TASKS}?surprise=true").status_code == 400
    assert (
        director.post(
            TASKS,
            {"title": "x", "description": "x" * 20_001},
            format="json",
        ).status_code
        == 400
    )
    assert (
        director.post(
            "/api/v1/tasks/auto-assign/",
            {"task_ids": [1, 1], "department": 1},
            format="json",
        ).status_code
        == 400
    )


def test_department_only_write_grant_does_not_expand_to_branch_backlog(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
    teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.filter(user=teacher_user, branch=branch).update(department=department)
    teacher = as_user(tenant_a, teacher_user)

    scoped = teacher.post(
        TASKS,
        {"title": "Department task", "department": department.pk},
        format="json",
    )
    assert scoped.status_code == 201, scoped.content
    branch_wide = teacher.post(
        TASKS,
        {"title": "Over-broad task", "branch": branch.pk},
        format="json",
    )
    assert branch_wide.status_code == 403
    assert branch_wide.json()["code"] == "out_of_scope"


def test_task_creator_uses_exact_principal_and_survives_bridge_deletion(
    tenant_a,
    client_for,
    django_assert_num_queries,
):
    from django.db import DatabaseError, transaction

    from apps.org.tests.factories import BranchFactory
    from apps.tasks.models import Task
    from apps.tasks.presenters import task_to_dict
    from apps.tasks.repositories.task_repository import TaskRepository
    from tests.role_principal_helpers import exact_session_client, shared_staff_teacher_bridge

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        user, teacher, staff = shared_staff_teacher_bridge(
            branch=branch,
            staff_role=Role.SUPPORT,
        )
        teacher_id = teacher.pk
    teacher_client = exact_session_client(
        client_for,
        tenant_a,
        user,
        principal_kind="teacher",
        principal_id=teacher_id,
    )
    response = teacher_client.post(
        TASKS,
        {"title": "Exact creator", "branch": branch.pk},
        format="json",
    )
    assert response.status_code == 201, response.content
    payload = response.json()["data"]
    assert payload["created_by"]["kind"] == "teacher"
    assert payload["created_by"]["id"] == teacher_id
    assert payload["created_by"]["display_name"]
    assert payload["created_by_attribution_status"] == "captured"

    with schema_context(tenant_a.schema_name):
        task_id = payload["id"]
        with pytest.raises(DatabaseError), transaction.atomic():
            Task.objects.filter(pk=task_id).update(created_by_principal_id=staff.pk)

        legacy = Task.objects.create(title="Ambiguous legacy creator", created_by=user)
        legacy_payload = task_to_dict(legacy)
        assert legacy_payload["created_by"] is None
        assert legacy_payload["created_by_attribution_status"] == "quarantined"

        from apps.teachers.models import TeacherProfile

        TeacherProfile.objects.filter(pk=teacher_id).update(is_active=False)
        exact = Task.objects.get(pk=task_id)
        exact.title = "Historical exact creator"
        exact.save()

        user.delete()
        with django_assert_num_queries(1):
            loaded = TaskRepository().get_queryset().get(pk=task_id)
            historical = task_to_dict(loaded)
        assert loaded.created_by_id is None
        assert historical["created_by"] == {
            "kind": "teacher",
            "id": teacher_id,
            "display_name": None,
            "account_label": "Teacher",
        }
        assert historical["created_by_attribution_status"] == "captured"


@pytest.mark.django_db(transaction=True)
def test_concurrent_terminal_transitions_serialize_on_the_task_row(tenant_a, monkeypatch):
    """The second transition must apply to the committed, locked task image."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout
    from threading import Event

    from django.db import close_old_connections

    from apps.tasks import services
    from apps.tasks.models import Task
    from core.exceptions import UnprocessableEntity

    with schema_context(tenant_a.schema_name):
        task = Task.objects.create(title="Concurrent transition")
        task_id = task.pk

    first_holds_lock = Event()
    release_first = Event()
    second_started = Event()
    original_save = Task.save

    def slow_first_save(instance, *args, **kwargs):
        if instance.pk == task_id and instance.status == Task.Status.DONE:
            first_holds_lock.set()
            if not release_first.wait(timeout=10):
                raise RuntimeError("test timed out waiting to release the first transition")
        return original_save(instance, *args, **kwargs)

    monkeypatch.setattr(Task, "save", slow_first_save)

    def transition(status: str, *, mark_started: bool = False):
        close_old_connections()
        try:
            with schema_context(tenant_a.schema_name):
                if mark_started:
                    second_started.set()
                stale = Task.objects.get(pk=task_id)
                try:
                    result = services.transition_task(
                        task=stale,
                        to_status=status,
                        actor=None,
                        actor_principal_kind="staff",
                        actor_principal_id=1,
                        can_transition_any=True,
                    )
                    return ("ok", result.status)
                except UnprocessableEntity as exc:
                    return ("invalid", exc.code)
        finally:
            close_old_connections()

    second = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(transition, Task.Status.DONE)
        try:
            assert first_holds_lock.wait(timeout=10)
            second = pool.submit(transition, Task.Status.CANCELLED, mark_started=True)
            assert second_started.wait(timeout=10)
            # The second worker has started but remains blocked on select_for_update.
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.25)
        finally:
            release_first.set()
        assert second is not None
        assert first.result(timeout=10) == ("ok", Task.Status.DONE)
        # DONE -> CANCELLED is a declared lifecycle edge.  Serializing on the row
        # means the second worker applies that edge to DONE instead of overwriting
        # a stale OPEN image; the sibling lifecycle test intentionally guarantees
        # this correction workflow remains available.
        assert second.result(timeout=10) == ("ok", Task.Status.CANCELLED)

    with schema_context(tenant_a.schema_name):
        assert Task.objects.get(pk=task_id).status == Task.Status.CANCELLED
        Task.objects.filter(pk=task_id).delete()
