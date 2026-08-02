"""F4-4 — in-app messaging: threads, messages, strict participant isolation,
student↔staff safeguarding, unread counts, and realtime notify."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.attachment_storage import AttachmentObjectError, VerifiedAttachment
from core.permissions import Role

pytestmark = pytest.mark.django_db

THREADS = "/api/v1/messaging/threads/"
UPLOAD = "/api/v1/messaging/attachments/upload-url/"
CONTACTS = "/api/v1/messaging/contacts/"
ROLE_PASSWORD = "Messaging-Principal-42"


def _rows(body):
    # Layered endpoints return {success, data, pagination}; a still-DRF endpoint returns
    # {count, next, previous, results}; some actions returned a bare list.
    if isinstance(body, dict):
        if "data" in body:
            return body["data"]
        if "results" in body:
            return body["results"]
    return body


def _attachment_key(client, monkeypatch, *, filename="photo.jpg", size=3, content_type="image/jpeg"):
    monkeypatch.setattr(
        "apps.messaging.services.presign_post_upload",
        lambda key, **kwargs: {"url": "https://storage.invalid/upload", "fields": {"key": key}},
    )
    response = client.post(
        UPLOAD,
        {"filename": filename, "size_bytes": size, "content_type": content_type},
        format="json",
    )
    assert response.status_code == 200, response.content
    key = response.json()["data"]["key"]
    monkeypatch.setattr(
        "apps.messaging.services.promote_attachment_object",
        lambda **_kwargs: VerifiedAttachment(
            size_bytes=size,
            content_type=content_type,
            sniffed_type=content_type,
        ),
    )
    return key


def _role_client(client_for, tenant, *, username: str):
    login = client_for(tenant)
    response = login.post(
        "/api/v1/auth/role-login/",
        {"username": username, "password": ROLE_PASSWORD},
        format="json",
    )
    assert response.status_code == 200, response.content
    client = client_for(tenant)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['data']['access']}")
    return client


def _multi_profile_student_network(tenant):
    """One bridge User with student scope A and director scope B.

    The student role login must retain only scope A. The staff profile and
    director membership intentionally make this a regression for bridge-wide
    role unioning in messaging services.
    """
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.parents.tests.factories import ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        local_branch = BranchFactory()
        remote_branch = BranchFactory()
        creator = UserFactory()
        student_account = StudentProfileFactory(
            user=creator,
            branch=local_branch,
            username="messaging.multi.student",
            phone="",
            email="",
        )
        staff_account = StaffProfile.objects.create(
            user=creator,
            username="messaging.multi.staff",
        )
        for account in (student_account, staff_account):
            account.set_password(ROLE_PASSWORD)
            account.save(update_fields=["password"])
        RoleMembership.objects.create(
            user=creator,
            branch=local_branch,
            role=Role.STUDENT,
        )
        RoleMembership.objects.create(
            user=creator,
            branch=remote_branch,
            role=Role.DIRECTOR,
        )

        local_staff = UserFactory()
        remote_staff = UserFactory()
        parent_user = UserFactory()
        RoleMembership.objects.create(
            user=local_staff,
            branch=local_branch,
            role=Role.REGISTRAR,
        )
        RoleMembership.objects.create(
            user=remote_staff,
            branch=remote_branch,
            role=Role.REGISTRAR,
        )
        RoleMembership.objects.create(
            user=parent_user,
            branch=local_branch,
            role=Role.PARENT,
        )
        for user, label in ((local_staff, "Local"), (remote_staff, "Remote")):
            StaffProfile.objects.create(
                user=user,
                username=f"messaging.{label.lower()}.staff",
                first_name=label,
            )
        ParentProfileFactory(
            user=parent_user,
            username="messaging.local.parent",
            phone="",
            email="",
        )
    return creator, local_staff, remote_staff, parent_user


def test_create_thread_and_exchange_messages(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)

    created = teacher_client.post(
        THREADS,
        {"subject": "Homework", "participant_ids": [student.id], "first_body": "Please submit."},
        format="json",
    )
    assert created.status_code == 201, created.content
    tid = created.json()["data"]["id"]

    # the student sees the thread
    assert any(t["id"] == tid for t in _rows(student_client.get(THREADS).json()))

    # the student replies
    reply = student_client.post(f"{THREADS}{tid}/messages/", {"body": "Done!"}, format="json")
    assert reply.status_code == 201, reply.content

    # both messages are visible to a participant
    msgs = _rows(teacher_client.get(f"{THREADS}{tid}/messages/").json())
    assert [m["body"] for m in msgs] == ["Please submit.", "Done!"]


def test_message_day_window_is_half_open_paginated_and_totally_ordered(tenant_a, as_role):
    from apps.messaging.models import Message

    teacher_client, teacher = as_role(Role.TEACHER)
    _student_client, student = as_role(Role.STUDENT)
    outsider_client, _outsider = as_role(Role.TEACHER)
    tid = teacher_client.post(THREADS, {"participant_ids": [student.id]}, format="json").json()["data"]["id"]
    lower = datetime(2026, 7, 21, tzinfo=UTC)
    upper = lower + timedelta(days=1)
    with schema_context(tenant_a.schema_name):
        first = Message.objects.create(thread_id=tid, sender=teacher, body="first at boundary")
        second = Message.objects.create(thread_id=tid, sender=teacher, body="second same instant")
        excluded = Message.objects.create(thread_id=tid, sender=teacher, body="next day boundary")
        Message.objects.filter(pk__in=(first.pk, second.pk)).update(created_at=lower)
        Message.objects.filter(pk=excluded.pk).update(created_at=upper)

    url = f"{THREADS}{tid}/messages/"
    bounds = {
        "created_at_gte": lower.isoformat(),
        "created_at_lt": upper.isoformat(),
        "page_size": 1,
    }
    page_one = teacher_client.get(url, bounds)
    page_two = teacher_client.get(url, {**bounds, "page": 2})

    assert page_one.status_code == 200, page_one.content
    assert page_two.status_code == 200, page_two.content
    assert [row["id"] for row in _rows(page_one.json())] == [first.pk]
    assert [row["id"] for row in _rows(page_two.json())] == [second.pk]
    assert page_one.json()["pagination"]["total"] == 2
    assert page_one.json()["pagination"]["pages"] == 2
    assert excluded.pk not in {row["id"] for row in _rows(page_one.json()) + _rows(page_two.json())}
    # Scope is resolved before range processing: a guessed thread stays hidden.
    assert outsider_client.get(url, bounds).status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"created_at_gte": "2026-07-21T00:00:00Z"},
        {
            "created_at_gte": "2026-07-21T00:00:00",
            "created_at_lt": "2026-07-22T00:00:00",
        },
        {
            "created_at_gte": "2026-07-22T00:00:00Z",
            "created_at_lt": "2026-07-21T00:00:00Z",
        },
        {
            "created_at_gte": "2026-07-21T00:00:00Z",
            "created_at_lt": "2026-07-22T03:00:01Z",
        },
    ],
)
def test_message_day_window_rejects_partial_naive_reversed_or_unbounded_ranges(tenant_a, as_role, params):
    teacher_client, _teacher = as_role(Role.TEACHER)
    _student_client, student = as_role(Role.STUDENT)
    tid = teacher_client.post(THREADS, {"participant_ids": [student.id]}, format="json").json()["data"]["id"]

    response = teacher_client.get(f"{THREADS}{tid}/messages/", params)

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"


def test_non_participant_cannot_access_thread(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    _sc, student = as_role(Role.STUDENT)
    outsider_client, _o = as_role(Role.TEACHER)

    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "hi"}, format="json"
    ).json()["data"]["id"]

    # an outsider can neither read nor post in a thread they're not part of
    assert outsider_client.get(f"{THREADS}{tid}/").status_code == 404
    assert outsider_client.get(f"{THREADS}{tid}/messages/").status_code == 404
    assert outsider_client.post(f"{THREADS}{tid}/messages/", {"body": "x"}, format="json").status_code == 404
    assert _rows(outsider_client.get(THREADS).json()) == []


def test_student_cannot_message_another_student(tenant_a, as_role):
    student_client, _s = as_role(Role.STUDENT)
    _oc, other_student = as_role(Role.STUDENT)
    r = student_client.post(
        THREADS, {"participant_ids": [other_student.id], "first_body": "hey"}, format="json"
    )
    assert r.status_code == 403
    assert r.json()["code"] == "non_staff_recipient"


def test_student_can_message_a_teacher(tenant_a, as_role):
    student_client, _s = as_role(Role.STUDENT)
    _tc, teacher = as_role(Role.TEACHER)
    r = student_client.post(
        THREADS, {"participant_ids": [teacher.id], "first_body": "a question"}, format="json"
    )
    assert r.status_code == 201, r.content


def test_unread_count_tracks_reads(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "m1"}, format="json"
    ).json()["data"]["id"]

    def student_unread():
        return next(t["unread_count"] for t in _rows(student_client.get(THREADS).json()) if t["id"] == tid)

    assert student_unread() == 1  # the teacher's opener
    student_client.post(f"{THREADS}{tid}/read/", {}, format="json")
    assert student_unread() == 0  # caught up
    teacher_client.post(f"{THREADS}{tid}/messages/", {"body": "m2"}, format="json")
    assert student_unread() == 1  # a new one arrived


def test_message_notifies_the_recipient(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    teacher_client.post(THREADS, {"participant_ids": [student.id], "first_body": "ping"}, format="json")
    events = {n["event_type"] for n in student_client.get("/api/v1/notifications/").json()["results"]}
    assert "message.received" in events


def test_empty_message_rejected(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    _sc, student = as_role(Role.STUDENT)
    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "hi"}, format="json"
    ).json()["data"]["id"]
    assert teacher_client.post(f"{THREADS}{tid}/messages/", {"body": "   "}, format="json").status_code == 400


def test_thread_needs_another_participant(tenant_a, as_role):
    teacher_client, teacher = as_role(Role.TEACHER)
    # a thread with only yourself is rejected
    r = teacher_client.post(THREADS, {"participant_ids": [teacher.id], "first_body": "note"}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "thread_needs_participant"


def test_role_without_messaging_is_denied(tenant_a, as_role):
    sec_client, _s = as_role(Role.SECURITY)  # security holds no messaging permission
    assert sec_client.get(THREADS).status_code == 403
    assert sec_client.get(CONTACTS).status_code == 403
    assert (
        sec_client.post(THREADS, {"participant_ids": [1], "first_body": "x"}, format="json").status_code
        == 403
    )


def test_teacher_contact_directory_and_thread_scope(tenant_a, user_in, as_user):
    """The directory and create policy share one fail-closed recipient scope."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()

    teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    staff_user = user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch)
    colleague_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    manager_user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    custom_manager_user = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    own_student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    untaught_student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    withdrawn_student_user = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    parent_user = user_in(tenant_a, roles=[Role.PARENT], branch=branch)

    with schema_context(tenant_a.schema_name):
        from apps.access.models import AccountType

        custom_manager_type = AccountType.objects.create(
            name="School manager",
            slug="school-manager",
            account_kind=AccountType.AccountKind.STAFF,
        )
        custom_manager_membership = custom_manager_user.role_memberships.get()
        custom_manager_membership.account_type = custom_manager_type
        custom_manager_membership.save(update_fields=["account_type"])
        teacher = TeacherProfileFactory(
            user=teacher_user,
            branch=branch,
            first_name="Scope",
            last_name="Teacher",
        )
        colleague = TeacherProfileFactory(user=colleague_user, branch=branch)
        taught = CohortFactory(branch=branch, primary_teacher=teacher)
        untaught = CohortFactory(branch=branch)
        own_student = StudentProfileFactory(
            user=own_student_user,
            branch=branch,
            current_cohort=taught,
            first_name="Own",
            last_name="Student",
            status=StudentProfile.Status.ACTIVE,
        )
        StudentProfileFactory(
            user=untaught_student_user,
            branch=branch,
            current_cohort=untaught,
            status=StudentProfile.Status.ACTIVE,
        )
        StudentProfileFactory(
            user=withdrawn_student_user,
            branch=branch,
            current_cohort=taught,
            status=StudentProfile.Status.WITHDRAWN,
        )
        StaffProfile.objects.create(
            user=staff_user,
            username=staff_user.username,
            password=staff_user.password,
            first_name="Helpful",
            last_name="Registrar",
        )
        StaffProfile.objects.create(
            user=manager_user,
            username=manager_user.username,
            password=manager_user.password,
            first_name="Hidden",
            last_name="Manager",
        )
        StaffProfile.objects.create(
            user=custom_manager_user,
            username=custom_manager_user.username,
            password=custom_manager_user.password,
            first_name="Custom",
            last_name="Manager",
        )

    client = as_user(tenant_a, teacher_user)
    response = client.get(CONTACTS)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["pagination"]["self_user_id"] == teacher_user.id
    rows = body["data"]
    ids = {row["user_id"] for row in rows}
    assert {staff_user.id, colleague_user.id, own_student_user.id} <= ids
    assert teacher_user.id not in ids
    assert manager_user.id not in ids
    assert custom_manager_user.id not in ids
    assert untaught_student_user.id not in ids
    assert withdrawn_student_user.id not in ids
    assert parent_user.id not in ids

    student_row = next(row for row in rows if row["user_id"] == own_student_user.id)
    assert student_row == {
        "id": own_student_user.id,
        "user_id": own_student_user.id,
        "principal_kind": "student",
        "category": "student",
        "profile_id": own_student.id,
        "display_name": "Own Student",
        "username": own_student.username,
        "role_label": "Student",
        "role_slug": Role.STUDENT,
        "is_online": False,
        "is_online_is_heuristic": True,
        "recently_active": False,
        "presence_source": "last_seen_within_5_minutes",
        "activity_status": "not_recently_active",
        "activity_status_is_presence": False,
    }
    assert "phone" not in student_row
    assert "email" not in student_row

    colleague_row = next(row for row in rows if row["user_id"] == colleague_user.id)
    assert colleague_row["profile_id"] == colleague.id
    assert colleague_row["principal_kind"] == "teacher"
    assert client.get(f"{CONTACTS}?category=student&search=Own").json()["data"] == [student_row]

    created_for_student = client.post(
        THREADS,
        {"participant_ids": [own_student_user.id], "first_body": "Welcome"},
        format="json",
    )
    assert created_for_student.status_code == 201, created_for_student.content
    participant_ids = {
        participant["user"] for participant in created_for_student.json()["data"]["participants"]
    }
    assert participant_ids == {teacher_user.id, own_student_user.id}

    created_for_staff = client.post(
        THREADS,
        {"participant_ids": [staff_user.id], "first_body": "Hello"},
        format="json",
    )
    assert created_for_staff.status_code == 201, created_for_staff.content

    for forbidden_id in (
        untaught_student_user.id,
        withdrawn_student_user.id,
        parent_user.id,
        manager_user.id,
        custom_manager_user.id,
    ):
        denied = client.post(
            THREADS,
            {"participant_ids": [forbidden_id], "first_body": "guess"},
            format="json",
        )
        assert denied.status_code == 403, denied.content
        assert denied.json()["code"] == "recipient_out_of_scope"


def test_non_teacher_staff_cannot_guess_cross_branch_recipient_ids(tenant_a, user_in, as_user):
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
    creator = user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch_a)
    local = user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch_a)
    remote = user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch_b)
    with schema_context(tenant_a.schema_name):
        for user, label in ((creator, "Creator"), (local, "Local"), (remote, "Remote")):
            StaffProfile.objects.create(
                user=user,
                username=user.username,
                password=user.password,
                first_name=label,
            )

    client = as_user(tenant_a, creator)
    allowed = client.post(
        THREADS,
        {"participant_ids": [local.id], "first_body": "local"},
        format="json",
    )
    assert allowed.status_code == 201, allowed.content

    denied = client.post(
        THREADS,
        {"participant_ids": [remote.id], "first_body": "guessed id"},
        format="json",
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "recipient_out_of_scope"


def test_recipient_scope_does_not_borrow_an_unrelated_department_membership(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        permitted_department = DepartmentFactory(branch=branch)
        unrelated_department = DepartmentFactory(branch=branch)
    creator = user_in(tenant_a)
    local = user_in(tenant_a)
    remote = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        # Only the registrar assignment grants messaging:write. The security
        # assignment must not lend its department to that separate grant.
        RoleMembership.objects.create(
            user=creator,
            branch=branch,
            department=permitted_department,
            role=Role.REGISTRAR,
        )
        RoleMembership.objects.create(
            user=creator,
            branch=branch,
            department=unrelated_department,
            role=Role.SECURITY,
        )
        RoleMembership.objects.create(
            user=local,
            branch=branch,
            department=permitted_department,
            role=Role.REGISTRAR,
        )
        RoleMembership.objects.create(
            user=remote,
            branch=branch,
            department=unrelated_department,
            role=Role.REGISTRAR,
        )
        for user, label in ((creator, "Creator"), (local, "Local"), (remote, "Remote")):
            StaffProfile.objects.create(
                user=user,
                username=user.username,
                password=user.password,
                first_name=label,
            )
            user.refresh_from_db()

    client = as_user(tenant_a, creator)
    allowed = client.post(
        THREADS,
        {"participant_ids": [local.pk], "first_body": "local"},
        format="json",
    )
    denied = client.post(
        THREADS,
        {"participant_ids": [remote.pk], "first_body": "guessed id"},
        format="json",
    )

    assert allowed.status_code == 201, allowed.content
    assert denied.status_code == 403
    assert denied.json()["code"] == "recipient_out_of_scope"


def test_teacher_membership_without_profile_cannot_bypass_recipient_directory(
    tenant_a,
    user_in,
    client_for,
):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from core.session_auth import create_session

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    teacher = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    student = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        StudentProfileFactory(user=student, branch=branch)
        invalid_session = create_session(
            teacher,
            principal_kind="teacher",
            principal_id=9_223_372_036_854_775_807,
        )

    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {invalid_session.key}")
    response = client.post(
        THREADS,
        {"participant_ids": [student.id], "first_body": "profile bypass"},
        format="json",
    )
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_failed"


def test_student_session_cannot_borrow_multi_profile_staff_scope(tenant_a, client_for):
    creator, local_staff, remote_staff, _parent = _multi_profile_student_network(tenant_a)
    client = _role_client(
        client_for,
        tenant_a,
        username="messaging.multi.student",
    )

    contacts = client.get(CONTACTS)
    assert contacts.status_code == 200, contacts.content
    contact_ids = {row["user_id"] for row in contacts.json()["data"]}
    assert local_staff.pk in contact_ids
    assert remote_staff.pk not in contact_ids

    allowed = client.post(
        THREADS,
        {"participant_ids": [local_staff.pk], "first_body": "local student question"},
        format="json",
    )
    denied = client.post(
        THREADS,
        {"participant_ids": [remote_staff.pk], "first_body": "borrowed director scope"},
        format="json",
    )

    assert allowed.status_code == 201, allowed.content
    assert creator.pk in {participant["user"] for participant in allowed.json()["data"]["participants"]}
    assert denied.status_code == 403
    assert denied.json()["code"] == "recipient_out_of_scope"


def test_student_session_uses_student_safeguarding_on_multi_profile_user(
    tenant_a,
    client_for,
):
    _creator, _local_staff, _remote_staff, parent = _multi_profile_student_network(tenant_a)
    client = _role_client(
        client_for,
        tenant_a,
        username="messaging.multi.student",
    )

    response = client.post(
        THREADS,
        {"participant_ids": [parent.pk], "first_body": "unsafe non-staff pairing"},
        format="json",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "non_staff_recipient"


def test_role_native_staff_and_teacher_messaging_flows_remain_available(
    tenant_a,
    client_for,
):
    from apps.cohorts.tests.factories import CohortFactory, CohortTeacherFactory
    from apps.org.models import StaffProfile
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        staff_creator = UserFactory()
        staff_recipient = UserFactory()
        teacher_user = UserFactory()
        student_user = UserFactory()
        for user, role in (
            (staff_creator, Role.REGISTRAR),
            (staff_recipient, Role.REGISTRAR),
            (teacher_user, Role.TEACHER),
            (student_user, Role.STUDENT),
        ):
            RoleMembership.objects.create(user=user, branch=branch, role=role)

        staff_account = StaffProfile.objects.create(
            user=staff_creator,
            username="messaging.normal.staff",
        )
        StaffProfile.objects.create(
            user=staff_recipient,
            username="messaging.staff.recipient",
        )
        teacher_account = TeacherProfileFactory(
            user=teacher_user,
            branch=branch,
            username="messaging.normal.teacher",
            phone="",
            email="",
        )
        for account in (staff_account, teacher_account):
            account.set_password(ROLE_PASSWORD)
            account.save(update_fields=["password"])

        cohort = CohortFactory(branch=branch)
        CohortTeacherFactory(cohort=cohort, teacher=teacher_account)
        StudentProfileFactory(
            user=student_user,
            branch=branch,
            current_cohort=cohort,
            phone="",
            email="",
        )

    staff_client = _role_client(
        client_for,
        tenant_a,
        username="messaging.normal.staff",
    )
    teacher_client = _role_client(
        client_for,
        tenant_a,
        username="messaging.normal.teacher",
    )

    staff_thread = staff_client.post(
        THREADS,
        {"participant_ids": [staff_recipient.pk], "first_body": "staff flow"},
        format="json",
    )
    teacher_thread = teacher_client.post(
        THREADS,
        {"participant_ids": [student_user.pk], "first_body": "teacher flow"},
        format="json",
    )
    assert staff_thread.status_code == 201, staff_thread.content
    assert teacher_thread.status_code == 201, teacher_thread.content


# --------------------------------------------------------------------------- #
# review hardening
# --------------------------------------------------------------------------- #
def test_staff_cannot_open_a_two_student_thread(tenant_a, as_role):
    # The safeguarding invariant is on the participant SET, not just the opener:
    # even a teacher cannot co-locate two students (no unsupervised peer channel).
    teacher_client, _t = as_role(Role.TEACHER)
    _s1c, s1 = as_role(Role.STUDENT)
    _s2c, s2 = as_role(Role.STUDENT)
    r = teacher_client.post(
        THREADS, {"participant_ids": [s1.id, s2.id], "first_body": "group"}, format="json"
    )
    assert r.status_code == 403
    assert r.json()["code"] == "non_staff_recipient"


def test_safeguarding_uses_canonical_account_kind_when_legacy_roles_drift(
    tenant_a,
    as_role,
):
    from apps.users.models import RoleMembership

    teacher_client, _teacher = as_role(Role.TEACHER)
    _first_client, first = as_role(Role.STUDENT)
    _second_client, second = as_role(Role.STUDENT)
    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.filter(user_id__in=(first.pk, second.pk)).update(role=Role.SUPPORT)

    response = teacher_client.post(
        THREADS,
        {
            "participant_ids": [first.pk, second.pk],
            "first_body": "This must remain blocked",
        },
        format="json",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "non_staff_recipient"


def test_teacher_parent_student_thread_allowed(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    _pc, parent = as_role(Role.PARENT)
    _sc, student = as_role(Role.STUDENT)
    # one student + a parent + the teacher is fine (a conference thread)
    r = teacher_client.post(
        THREADS, {"participant_ids": [parent.id, student.id], "first_body": "meeting"}, format="json"
    )
    assert r.status_code == 201, r.content


def test_revoking_messaging_write_makes_a_role_read_only(tenant_a, as_role):
    from apps.access.services import set_override

    teacher_client, _t = as_role(Role.TEACHER)
    _sc, student = as_role(Role.STUDENT)
    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "hi"}, format="json"
    ).json()["data"]["id"]

    with schema_context(tenant_a.schema_name):
        set_override(role=Role.TEACHER, permission="messaging:write", effect="revoke")

    # reading the thread still works...
    assert teacher_client.get(f"{THREADS}{tid}/messages/").status_code == 200
    # ...but posting is now denied (write was revoked)
    assert teacher_client.post(f"{THREADS}{tid}/messages/", {"body": "x"}, format="json").status_code == 403


def test_membershipless_participant_rejected(tenant_a, as_role):
    from apps.users.tests.factories import UserFactory

    teacher_client, _t = as_role(Role.TEACHER)
    with schema_context(tenant_a.schema_name):
        orphan = UserFactory.create()  # active user, but no RoleMembership in this center
    r = teacher_client.post(THREADS, {"participant_ids": [orphan.id], "first_body": "hi"}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "unknown_participant"


def test_attachment_only_message_allowed(tenant_a, as_role, monkeypatch):
    teacher_client, _t = as_role(Role.TEACHER)
    _sc, student = as_role(Role.STUDENT)
    key = _attachment_key(teacher_client, monkeypatch)
    created = teacher_client.post(
        THREADS,
        {"participant_ids": [student.id], "attachments": [key]},
        format="json",
    )
    assert created.status_code == 201, created.content
    msgs = _rows(teacher_client.get(f"{THREADS}{created.json()['data']['id']}/messages/").json())
    assert len(msgs) == 1
    assert len(msgs[0]["attachments"]) == 1
    assert msgs[0]["attachments"][0] != key
    assert "/messaging/messages/" in msgs[0]["attachments"][0]
    assert msgs[0]["body"] == ""


def test_unread_excludes_your_own_messages(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "hi"}, format="json"
    ).json()["data"]["id"]
    student_client.post(f"{THREADS}{tid}/read/", {}, format="json")
    student_client.post(f"{THREADS}{tid}/messages/", {"body": "my reply"}, format="json")
    rows = _rows(student_client.get(THREADS).json())
    assert next(t["unread_count"] for t in rows if t["id"] == tid) == 0  # own message isn't unread


def test_attachment_grant_is_owner_bound_and_single_use(tenant_a, as_role, monkeypatch):
    owner_client, _owner = as_role(Role.TEACHER)
    other_client, other = as_role(Role.TEACHER)
    key = _attachment_key(owner_client, monkeypatch)
    tid = owner_client.post(
        THREADS,
        {"participant_ids": [other.id], "first_body": "shared thread"},
        format="json",
    ).json()["data"]["id"]

    stolen = other_client.post(
        f"{THREADS}{tid}/messages/",
        {"attachments": [key]},
        format="json",
    )
    assert stolen.status_code == 422
    assert stolen.json()["code"] == "invalid_attachment_key"

    noncanonical = owner_client.post(
        f"{THREADS}{tid}/messages/",
        {"attachments": [f"  {key}  "]},
        format="json",
    )
    assert noncanonical.status_code == 400
    assert noncanonical.json()["code"] == "validation_error"

    sent = owner_client.post(
        f"{THREADS}{tid}/messages/",
        {"attachments": [key]},
        format="json",
    )
    assert sent.status_code == 201, sent.content
    assert sent.json()["data"]["attachments"] != [key]
    assert "/messaging/messages/" in sent.json()["data"]["attachments"][0]

    replay = owner_client.post(
        f"{THREADS}{tid}/messages/",
        {"attachments": [key]},
        format="json",
    )
    assert replay.status_code == 422
    assert replay.json()["code"] == "invalid_attachment_grant"


def test_expired_attachment_grant_is_rejected(tenant_a, as_role, monkeypatch):
    from apps.messaging.models import MessageAttachmentUploadGrant

    teacher_client, _teacher = as_role(Role.TEACHER)
    _student_client, student = as_role(Role.STUDENT)
    key = _attachment_key(teacher_client, monkeypatch)
    with schema_context(tenant_a.schema_name):
        MessageAttachmentUploadGrant.objects.filter(key=key).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

    response = teacher_client.post(
        THREADS,
        {"participant_ids": [student.id], "attachments": [key]},
        format="json",
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_attachment_grant"


@pytest.mark.parametrize(
    ("metadata", "expected_code"),
    [
        ({"ContentLength": 4, "ContentType": "image/jpeg"}, "attachment_size_mismatch"),
        ({"ContentLength": 3, "ContentType": "image/png"}, "attachment_type_mismatch"),
    ],
)
def test_uploaded_attachment_metadata_must_match_grant(
    tenant_a, as_role, monkeypatch, metadata, expected_code
):
    teacher_client, _teacher = as_role(Role.TEACHER)
    _student_client, student = as_role(Role.STUDENT)
    key = _attachment_key(teacher_client, monkeypatch)
    reason = "size" if metadata["ContentLength"] != 3 else "content_type"

    def mismatch(**_kwargs):
        raise AttachmentObjectError(reason)

    monkeypatch.setattr("apps.messaging.services.promote_attachment_object", mismatch)

    response = teacher_client.post(
        THREADS,
        {"participant_ids": [student.id], "attachments": [key]},
        format="json",
    )
    assert response.status_code == 422
    assert response.json()["code"] == expected_code


def test_attachment_must_be_uploaded_before_message(tenant_a, as_role, monkeypatch):
    teacher_client, _teacher = as_role(Role.TEACHER)
    _student_client, student = as_role(Role.STUDENT)
    key = _attachment_key(teacher_client, monkeypatch)

    def missing(**_kwargs):
        raise AttachmentObjectError("missing")

    monkeypatch.setattr("apps.messaging.services.promote_attachment_object", missing)
    response = teacher_client.post(
        THREADS,
        {"participant_ids": [student.id], "attachments": [key]},
        format="json",
    )
    assert response.status_code == 422
    assert response.json()["code"] == "attachment_not_uploaded"


def test_attachment_download_requires_thread_participation(tenant_a, as_role, monkeypatch):
    teacher_client, _teacher = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    outsider_client, _outsider = as_role(Role.TEACHER)
    key = _attachment_key(teacher_client, monkeypatch)
    created = teacher_client.post(
        THREADS,
        {"participant_ids": [student.id], "attachments": [key]},
        format="json",
    )
    tid = created.json()["data"]["id"]
    messages = _rows(teacher_client.get(f"{THREADS}{tid}/messages/").json())
    stored_key = messages[0]["attachments"][0]
    monkeypatch.setattr(
        "apps.messaging.services.presign_download",
        lambda stored_key, **kwargs: f"https://storage.invalid/download/{stored_key}",
    )
    url = f"{THREADS}{tid}/attachments/download/"

    participant = student_client.get(url, {"key": stored_key})
    assert participant.status_code == 200
    assert stored_key in participant.json()["data"]["url"]
    assert student_client.head(url, {"key": stored_key}).status_code == 200
    assert outsider_client.get(url, {"key": stored_key}).status_code == 404


def test_attachment_null_and_excess_are_clean_400s(tenant_a, as_role):
    teacher_client, _teacher = as_role(Role.TEACHER)
    _student_client, student = as_role(Role.STUDENT)
    assert (
        teacher_client.post(
            THREADS,
            {"participant_ids": [student.id], "first_body": "x", "attachments": None},
            format="json",
        ).status_code
        == 400
    )
    assert (
        teacher_client.post(
            THREADS,
            {"participant_ids": [student.id], "attachments": [f"key-{i}" for i in range(11)]},
            format="json",
        ).status_code
        == 400
    )


def test_thread_detail_pagination_ordering_and_head(tenant_a, as_role):
    teacher_client, _teacher = as_role(Role.TEACHER)
    _student_client, student = as_role(Role.STUDENT)
    created = teacher_client.post(
        THREADS,
        {"subject": "  Homework  ", "participant_ids": [student.id], "first_body": "  first  "},
        format="json",
    )
    assert created.status_code == 201, created.content
    data = created.json()["data"]
    tid = data["id"]
    assert data["subject"] == "Homework"
    assert data["last_message_at"] is not None
    teacher_client.post(f"{THREADS}{tid}/messages/", {"body": "second"}, format="json")

    detail = teacher_client.get(f"{THREADS}{tid}/")
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == tid
    page = teacher_client.get(f"{THREADS}{tid}/messages/?page=2&page_size=1")
    assert page.status_code == 200
    assert [row["body"] for row in page.json()["data"]] == ["second"]
    assert teacher_client.get(f"{THREADS}?ordering=-created_at&page_size=1").status_code == 200
    invalid_ordering = teacher_client.get(f"{THREADS}?ordering=--created_at")
    assert invalid_ordering.status_code == 400
    assert invalid_ordering.json()["errors"] == {"ordering": ["Invalid value."]}
    assert teacher_client.head(THREADS).status_code == 200
    assert teacher_client.head(f"{THREADS}{tid}/").status_code == 200
    assert teacher_client.head(f"{THREADS}{tid}/messages/").status_code == 200


def test_thread_create_role_lookup_query_count_is_bounded(tenant_a, user_in, django_assert_max_num_queries):
    from apps.messaging.services import create_thread
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.tests.factories import RoleMembershipFactory, UserFactory

    creator = user_in(tenant_a, roles=[Role.TEACHER])
    with schema_context(tenant_a.schema_name):
        TeacherProfileFactory(user=creator)
        participants = []
        for _ in range(40):
            participant = UserFactory()
            RoleMembershipFactory(user=participant, role=Role.TEACHER)
            TeacherProfileFactory(user=participant)
            participants.append(participant)

        # Resolution performs one batch query per role-principal table, never one
        # lookup per participant. Permission scope checks and both writes are also
        # bounded, so forty recipients still have a constant query budget.
        with django_assert_max_num_queries(14):
            thread = create_thread(creator=creator, participants=participants)
        assert thread.participants.count() == 41
