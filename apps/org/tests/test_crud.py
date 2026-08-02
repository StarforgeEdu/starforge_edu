"""Org CRUD over the layered (off-DRF) views: branch create/detail via the API,
the read-only transfers endpoint, and department branch-scoping. Complements
test_org_domain (rooms/hours/holidays/archive), test_settings, test_departments."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db


def test_branch_create_list_retrieve_update(as_role):
    client, _ = as_role(Role.DIRECTOR)

    resp = client.post("/api/v1/org/branches/", {"name": "Downtown", "slug": "downtown"}, format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["success"] is True
    bid = body["data"]["id"]
    assert body["data"]["name"] == "Downtown"
    assert body["data"]["departments"] == []
    assert body["data"]["working_hours"] == []

    listed = client.get("/api/v1/org/branches/").json()
    assert "pagination" in listed
    assert any(b["id"] == bid for b in listed["data"])

    detail = client.get(f"/api/v1/org/branches/{bid}/").json()["data"]
    assert detail["id"] == bid
    assert "capacity_status" in detail  # detail-only field

    upd = client.patch(f"/api/v1/org/branches/{bid}/", {"phone": "+998901112233"}, format="json")
    assert upd.status_code == 200
    assert upd.json()["data"]["phone"] == "+998901112233"


def test_branch_create_requires_name_and_slug(as_role):
    """DRF's ModelSerializer enforced required fields; the layered create must too."""
    client, _ = as_role(Role.DIRECTOR)
    resp = client.post("/api/v1/org/branches/", {"name": "NoSlug"}, format="json")
    assert resp.status_code == 400
    assert "slug" in resp.json()["errors"]


def test_branch_rejects_invalid_slug(as_role):
    client, _ = as_role(Role.DIRECTOR)
    resp = client.post("/api/v1/org/branches/", {"name": "X", "slug": "not a slug!"}, format="json")
    assert resp.status_code == 400
    assert "slug" in resp.json()["errors"]


def test_room_capacity_out_of_range_is_400_not_500(as_role, tenant_a):
    """An out-of-range capacity (> PositiveSmallInteger max) must be a clean 400,
    never a 500 DataError from Postgres."""
    from apps.org.tests.factories import BranchFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = client.post(
        "/api/v1/org/rooms/", {"branch": branch.id, "name": "Huge", "capacity": 99999}, format="json"
    )
    assert resp.status_code == 400


def test_branch_write_denied_for_teacher(as_role, tenant_a):
    """Teacher holds org:read (GET 200) but not org:write (create 403)."""
    client, _ = as_role(Role.TEACHER)
    assert client.get("/api/v1/org/branches/").status_code == 200
    resp = client.post("/api/v1/org/branches/", {"name": "X", "slug": "x"}, format="json")
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


def test_transfer_history_is_readable_but_has_no_generic_update(as_role, tenant_a):
    from apps.org.services import record_transfer
    from apps.org.tests.factories import BranchFactory
    from apps.users.tests.factories import UserFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        a = BranchFactory()
        b = BranchFactory()
        record_transfer(user=UserFactory(), from_branch=a, to_branch=b, reason="moved")

    listed = client.get("/api/v1/org/transfers/").json()
    assert "pagination" in listed
    assert len(listed["data"]) >= 1
    assert listed["data"][0]["from_branch"] == a.id
    # The collection's only write is the dedicated student-transfer POST; generic
    # detail/list updates remain unavailable.
    assert client.put("/api/v1/org/transfers/", {}, format="json").status_code == 405


def test_department_list_surfaces_readable_fk_names(as_role, tenant_a):
    """The departments list must carry branch_name + head_name next to the bare
    branch/head ids so a client needn't make a second call. branch/head are
    select_related on the list queryset, so this adds JOINs, not queries."""
    from apps.org.services import set_department_head
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.teachers.services import create_teacher

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(name="Central Campus")
        dept = DepartmentFactory(branch=branch)
        teacher = create_teacher(branch=branch, phone="+998905559050", first_name="Dana")
        set_department_head(dept, teacher)
        expected_head = teacher.get_full_name()
        head_teacher_id = teacher.id

    row = next(d for d in client.get("/api/v1/org/departments/").json()["data"] if d["id"] == dept.id)
    assert row["branch"] == branch.id
    assert row["branch_name"] == "Central Campus"
    assert row["head"] == head_teacher_id
    assert row["head_name"] == expected_head


def test_department_list_and_detail_branch_scoped(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        mine = DepartmentFactory(branch=branch_a)
        theirs = DepartmentFactory(branch=branch_b)
    # A teacher (org:read, non-director) scoped to branch_a sees only branch_a depts.
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch_a))
    ids = {d["id"] for d in client.get("/api/v1/org/departments/").json()["data"]}
    assert mine.id in ids
    assert theirs.id not in ids
    # A cross-branch id is indistinguishable from a nonexistent record.
    assert client.get(f"/api/v1/org/departments/{theirs.id}/").status_code == 404


def test_branch_list_does_not_expose_cross_branch_rows_or_departments(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        mine = BranchFactory()
        other = BranchFactory()
        DepartmentFactory(branch=other, name="Private department")
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=mine))

    branches = client.get("/api/v1/org/branches/").json()["data"]
    assert {row["id"] for row in branches} == {mine.id}
    assert other.id not in {row["id"] for row in branches}


def test_head_of_department_org_read_is_exactly_membership_scoped_and_read_only(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory, DepartmentFactory, RoomFactory

    with schema_context(tenant_a.schema_name):
        local_branch = BranchFactory(name="Head's campus")
        remote_branch = BranchFactory(name="Private campus")
        local_department = DepartmentFactory(branch=local_branch, name="Head's department")
        remote_department = DepartmentFactory(branch=remote_branch, name="Private department")
        local_room = RoomFactory(branch=local_branch, name="Head's room")
        remote_room = RoomFactory(branch=remote_branch, name="Private room")
        user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=local_branch)

    client = as_user(tenant_a, user)

    branches = client.get("/api/v1/org/branches/")
    assert branches.status_code == 200, branches.content
    assert {row["id"] for row in branches.json()["data"]} == {local_branch.pk}

    departments = client.get("/api/v1/org/departments/")
    assert departments.status_code == 200, departments.content
    assert {row["id"] for row in departments.json()["data"]} == {local_department.pk}

    rooms = client.get("/api/v1/org/rooms/")
    assert rooms.status_code == 200, rooms.content
    assert {row["id"] for row in rooms.json()["data"]} == {local_room.pk}

    # Out-of-scope records are indistinguishable from absent records. The read
    # grant never expands into a tenant-wide organization directory.
    assert client.get(f"/api/v1/org/branches/{remote_branch.pk}/").status_code == 404
    assert client.get(f"/api/v1/org/departments/{remote_department.pk}/").status_code == 404
    assert client.get(f"/api/v1/org/rooms/{remote_room.pk}/").status_code == 404

    # The migration supplies only org:read. It neither adds org:write nor lets a
    # branch-scoped manager mint or mutate organization structure.
    assert (
        client.patch(
            f"/api/v1/org/branches/{local_branch.pk}/",
            {"name": "Unauthorized rename"},
            format="json",
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/org/departments/",
            {"branch": local_branch.pk, "name": "Unauthorized", "slug": "unauthorized"},
            format="json",
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/org/rooms/",
            {"branch": local_branch.pk, "name": "Unauthorized"},
            format="json",
        ).status_code
        == 403
    )


def test_transfer_audit_is_scoped_to_permission_membership(tenant_a, user_in, as_user):
    from apps.org.services import record_transfer
    from apps.org.tests.factories import BranchFactory
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        mine = BranchFactory()
        other = BranchFactory()
        third = BranchFactory()
        visible = record_transfer(user=UserFactory(), from_branch=mine, to_branch=other, reason="visible")
        hidden = record_transfer(user=UserFactory(), from_branch=other, to_branch=third, reason="hidden")
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=mine))

    response = client.get("/api/v1/org/transfers/")
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["data"]}
    assert visible.id in ids
    assert hidden.id not in ids
    assert client.get(f"/api/v1/org/transfers/{hidden.id}/").status_code == 404


def test_student_transfer_updates_scope_cohorts_and_visible_audit(tenant_a, client_for):
    from apps.access.models import AccountType
    from apps.cohorts.models import CohortMembership
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.models import BranchTransfer
    from apps.org.services import create_staff_account
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.services import ensure_role_membership
    from core.session_auth import create_session

    with schema_context(tenant_a.schema_name):
        source = BranchFactory(name="Source")
        target = BranchFactory(name="Target")
        first = CohortFactory(branch=source, name="First")
        second = CohortFactory(branch=source, name="Second")
        student = StudentProfileFactory(branch=source, current_cohort=first)
        CohortMembership.objects.create(cohort=first, student=student, start_date="2026-01-01")
        CohortMembership.objects.create(cohort=second, student=student, start_date="2026-02-01")
        canonical = ensure_role_membership(student, branch=source, role=Role.STUDENT)
        assert canonical.account_type.account_kind == AccountType.AccountKind.STUDENT
        actor = create_staff_account(
            branch=source,
            role=Role.DIRECTOR,
            username="scope-transfer-director",
        )
        session = create_session(
            actor.user,
            principal_kind="staff",
            principal_id=actor.pk,
        )

    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")

    response = client.post(
        "/api/v1/org/transfers/",
        {"student": student.pk, "to_branch": target.pk, "reason": "family moved"},
        format="json",
    )
    assert response.status_code == 201, response.content
    payload = response.json()["data"]
    assert payload["student"] == student.pk
    assert payload["student_public_id"] == student.student_id
    assert payload["student_attribution_status"] == "resolved"
    assert payload["from_branch"] == source.pk
    assert payload["to_branch"] == target.pk
    # The bridge User ids remain internal implementation details.
    assert "user" not in payload
    assert "actor" not in payload

    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        canonical.refresh_from_db()
        assert student.branch_id == target.pk
        assert student.current_cohort_id is None
        assert canonical.branch_id == target.pk
        assert canonical.department_id is None
        ended = CohortMembership.objects.filter(student=student).order_by("pk")
        assert ended.count() == 2
        assert all(row.end_date is not None for row in ended)
        assert {row.moved_reason for row in ended} == {"family moved"}
        assert (
            not RoleMembership.objects.filter(
                user_id=student.user_id,
                revoked_at__isnull=True,
                account_type__account_kind=AccountType.AccountKind.STUDENT,
            )
            .exclude(branch=target)
            .exists()
        )
        transfer = BranchTransfer.objects.get(pk=payload["id"])

    history = client.get("/api/v1/org/transfers/")
    assert history.status_code == 200
    assert transfer.pk in {row["id"] for row in history.json()["data"]}


@pytest.mark.parametrize("grant_target", [False, True])
def test_student_transfer_requires_write_scope_on_both_branches(
    tenant_a,
    as_user,
    grant_target,
):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.org.models import BranchTransfer
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        student = StudentProfileFactory(branch=source)
        operator = UserFactory()
        mover = AccountType.objects.create(
            name=f"Scoped mover {grant_target}",
            slug=f"scoped-mover-{str(grant_target).lower()}",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(account_type=mover, permission="org:read")
        AccountTypePermission.objects.create(account_type=mover, permission="org:write")
        RoleMembership.objects.create(
            user=operator,
            branch=target if grant_target else source,
            role=Role.SUPPORT,
            account_type=mover,
        )
        operator.refresh_from_db()
    client = as_user(tenant_a, operator)

    response = client.post(
        "/api/v1/org/transfers/",
        {"student": student.pk, "to_branch": target.pk, "reason": "not permitted"},
        format="json",
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    with schema_context(tenant_a.schema_name):
        student.refresh_from_db()
        assert student.branch_id == source.pk
        assert not BranchTransfer.objects.filter(user_id=student.user_id).exists()


def test_student_transfer_rolls_back_every_change_when_audit_write_fails(tenant_a, monkeypatch):
    from apps.cohorts.models import CohortMembership
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.models import BranchTransfer
    from apps.org.services import transfer_student
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.services import ensure_role_membership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        source = BranchFactory()
        target = BranchFactory()
        cohort = CohortFactory(branch=source)
        student = StudentProfileFactory(branch=source, current_cohort=cohort)
        membership = CohortMembership.objects.create(
            cohort=cohort,
            student=student,
            start_date="2026-01-01",
        )
        canonical = ensure_role_membership(student, branch=source, role=Role.STUDENT)
        actor = UserFactory()

        def _fail_audit(**_kwargs):
            raise RuntimeError("simulated audit storage failure")

        monkeypatch.setattr("apps.org.services.record_transfer", _fail_audit)
        with pytest.raises(RuntimeError, match="simulated audit storage failure"):
            transfer_student(
                student_id=student.pk,
                to_branch_id=target.pk,
                reason="rollback",
                actor=actor,
                allowed_branch_ids=None,
            )

        student.refresh_from_db()
        membership.refresh_from_db()
        canonical.refresh_from_db()
        assert student.branch_id == source.pk
        assert student.current_cohort_id == cohort.pk
        assert membership.end_date is None
        assert membership.moved_reason == ""
        assert canonical.branch_id == source.pk
        assert not BranchTransfer.objects.filter(user_id=student.user_id).exists()
