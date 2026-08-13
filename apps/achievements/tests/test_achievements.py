"""F15-2 — custom achievements: global (manager) / group (teacher) creation, the
teacher→manager global-approval flow, granting (with guards), the student/parent
wall, branch scope, and grants privacy."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

ACH = "/api/v1/achievements/"


def _rows(body):
    return body["data"] if isinstance(body, dict) and "data" in body else body


def _teacher_in_branch(tenant, user_in, as_user, branch):
    return as_user(tenant, user_in(tenant, roles=[Role.TEACHER], branch=branch))


def test_global_create_grant_and_student_wall(tenant_a, user_in, as_user, as_role):
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    student_user = user_in(tenant_a, roles=[Role.STUDENT])
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create(user=student_user)

    created = director.post(ACH, {"name": "Star Student", "scope": "global", "emoji": "⭐"}, format="json")
    assert created.status_code == 201, created.content
    assert created.json()["data"]["status"] == "active"  # a manager's global is live immediately
    aid = created.json()["data"]["id"]

    grant = director.post(f"{ACH}{aid}/grant/", {"student": student.id, "note": "Great term"}, format="json")
    assert grant.status_code == 201, grant.content

    student_client = as_user(tenant_a, student_user)
    rows = _rows(student_client.get(f"{ACH}mine/").json())
    assert len(rows) == 1
    assert rows[0]["achievement_detail"]["name"] == "Star Student"
    assert rows[0]["note"] == "Great term"


def test_teacher_group_active_and_global_request_approval(tenant_a, user_in, as_user, as_role):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
    teacher = _teacher_in_branch(tenant_a, user_in, as_user, branch)

    group = teacher.post(ACH, {"name": "Best Homework", "scope": "group", "cohort": cohort.id}, format="json")
    assert group.status_code == 201, group.content
    assert group.json()["data"]["status"] == "active"

    glob = teacher.post(ACH, {"name": "Center Champion", "scope": "global"}, format="json")
    assert glob.status_code == 201
    assert glob.json()["data"]["status"] == "pending"  # a teacher's global awaits a manager
    gid = glob.json()["data"]["id"]

    assert teacher.post(f"{ACH}{gid}/approve/", {}, format="json").status_code == 403  # can't self-approve
    approved = director.post(f"{ACH}{gid}/approve/", {}, format="json")
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "active"


def test_hod_can_approve_teacher_global_request(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    teacher = _teacher_in_branch(tenant_a, user_in, as_user, branch)
    hod = as_user(tenant_a, user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch))

    # a teacher requests a centre-wide (global) achievement -> pending
    gid = teacher.post(ACH, {"name": "Kindness", "scope": "global"}, format="json").json()["data"]["id"]
    # a HOD (not the director) holds achievements:approve — they must SEE the pending
    # request in their queue AND be able to approve it (the teacher->manager flow).
    listed = {r["id"] for r in _rows(hod.get(f"{ACH}?status=pending").json())}
    assert gid in listed
    approved = hod.post(f"{ACH}{gid}/approve/", {}, format="json")
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "active"


def test_dynamic_approve_only_role_can_see_and_action_pending_queue(tenant_a, user_in, as_user):
    """An override may grant approve independently of write; the scoped repository
    must not hide every approvable row from that valid actor."""
    from apps.access.services import set_override
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        set_override(role=Role.LIBRARIAN, permission="achievements:read", effect="grant")
        set_override(role=Role.LIBRARIAN, permission="achievements:approve", effect="grant")
    teacher = _teacher_in_branch(tenant_a, user_in, as_user, branch)
    approver = as_user(tenant_a, user_in(tenant_a, roles=[Role.LIBRARIAN], branch=branch))

    gid = teacher.post(ACH, {"name": "Service", "scope": "global"}, format="json").json()["data"]["id"]
    listed = {row["id"] for row in _rows(approver.get(f"{ACH}?status=pending").json())}
    assert gid in listed
    assert approver.post(f"{ACH}{gid}/approve/", {}, format="json").status_code == 200


def test_group_achievement_requires_a_cohort(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    r = teacher_client.post(ACH, {"name": "x", "scope": "group"}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "cohort_required"


def test_cross_branch_group_create_blocked(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
        cohort_b = CohortFactory.create(branch=branch_b)
    teacher_a = _teacher_in_branch(tenant_a, user_in, as_user, branch_a)
    # a teacher can't pin a group achievement to another branch's cohort
    r = teacher_a.post(ACH, {"name": "x", "scope": "group", "cohort": cohort_b.id}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "validation_error"
    assert r.json()["errors"] == {"cohort": ["Not found."]}

    missing = teacher_a.post(
        ACH,
        {"name": "Invisible group", "scope": "group", "cohort": 2_147_483_647},
        format="json",
    )
    assert missing.status_code == 400
    assert missing.json()["errors"] == {"cohort": ["Not found."]}


def test_grants_list_is_query_bounded(tenant_a, as_role, django_assert_max_num_queries):
    """R2-10: GET /achievements/<pk>/grants/ must not issue one query PER grant. The
    presenter dereferences g.achievement, so grants_of must select_related it — else a
    school-wide achievement granted to many students blows the query count linearly."""
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    aid = director.post(ACH, {"name": "Bounded", "scope": "global"}, format="json").json()["data"]["id"]
    with schema_context(tenant_a.schema_name):
        student_ids = [StudentProfileFactory.create().id for _ in range(6)]
    for sid in student_ids:
        assert director.post(f"{ACH}{aid}/grant/", {"student": sid}, format="json").status_code == 201
    # Constant regardless of the 6 grants on the page (base + one page query with the
    # FK joins) — the pre-fix N+1 would add one SELECT per grant row.
    with django_assert_max_num_queries(12):
        body = director.get(f"{ACH}{aid}/grants/").json()
    assert len(_rows(body)) == 6


def test_cross_branch_global_grant_blocked(tenant_a, user_in, as_user, as_role):
    """R2-07: a branch-scoped teacher must not grant a GLOBAL achievement to another
    branch's student. Existing foreign and unknown ids must be indistinguishable
    so the endpoint cannot serve as a student primary-key oracle."""
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
        student_b = StudentProfileFactory.create(branch=branch_b)
    # a director creates an ACTIVE global achievement (visible to all write-holders)
    aid = director.post(ACH, {"name": "School Star", "scope": "global"}, format="json").json()["data"]["id"]
    teacher_a = _teacher_in_branch(tenant_a, user_in, as_user, branch_a)
    r = teacher_a.post(f"{ACH}{aid}/grant/", {"student": student_b.id}, format="json")
    assert r.status_code == 404, r.content
    assert r.json()["code"] == "not_found"

    missing = teacher_a.post(
        f"{ACH}{aid}/grant/",
        {"student": 2_147_483_647},
        format="json",
    )
    assert missing.status_code == 404, missing.content
    assert missing.json()["code"] == "not_found"


def test_grant_guards(tenant_a, user_in, as_user, as_role):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        cohort = CohortFactory.create(branch=branch)
        member = StudentProfileFactory.create(branch=branch, current_cohort=cohort)
        outsider = StudentProfileFactory.create(branch=branch)
    teacher = _teacher_in_branch(tenant_a, user_in, as_user, branch)

    # a pending achievement cannot be granted
    pending_id = teacher.post(ACH, {"name": "P", "scope": "global"}, format="json").json()["data"]["id"]
    not_active = director.post(f"{ACH}{pending_id}/grant/", {"student": member.id}, format="json")
    assert not_active.status_code == 422
    assert not_active.json()["code"] == "achievement_not_active"

    grp_id = teacher.post(ACH, {"name": "G", "scope": "group", "cohort": cohort.id}, format="json").json()[
        "data"
    ]["id"]
    # a group achievement can't be granted to a non-member
    wrong = director.post(f"{ACH}{grp_id}/grant/", {"student": outsider.id}, format="json")
    assert wrong.status_code == 422
    assert wrong.json()["code"] == "student_not_in_group"
    # to a member -> ok; a second time -> 409
    assert director.post(f"{ACH}{grp_id}/grant/", {"student": member.id}, format="json").status_code == 201
    dup = director.post(f"{ACH}{grp_id}/grant/", {"student": member.id}, format="json")
    assert dup.status_code == 409
    assert dup.json()["code"] == "already_granted"


def test_reject_flow_then_not_grantable(tenant_a, user_in, as_user, as_role):
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    teacher_client, _t = as_role(Role.TEACHER)
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create()

    gid = teacher_client.post(ACH, {"name": "Maybe", "scope": "global"}, format="json").json()["data"]["id"]
    rejected = director.post(f"{ACH}{gid}/reject/", {}, format="json")
    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == "rejected"
    # re-deciding a non-pending achievement is rejected
    assert director.post(f"{ACH}{gid}/approve/", {}, format="json").status_code == 422
    # a rejected achievement cannot be granted
    g = director.post(f"{ACH}{gid}/grant/", {"student": student.id}, format="json")
    assert g.status_code == 422
    assert g.json()["code"] == "achievement_not_active"


def test_grants_action_is_staff_only(tenant_a, user_in, as_user, as_role):
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    student_user = user_in(tenant_a, roles=[Role.STUDENT])
    parent_user = user_in(tenant_a, roles=[Role.PARENT])
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory.create(user=student_user)
    aid = director.post(ACH, {"name": "Public Badge", "scope": "global"}, format="json").json()["data"]["id"]
    director.post(f"{ACH}{aid}/grant/", {"student": student.id}, format="json")

    # a student / parent must NOT enumerate who earned an achievement
    assert as_user(tenant_a, student_user).get(f"{ACH}{aid}/grants/").status_code == 403
    assert as_user(tenant_a, parent_user).get(f"{ACH}{aid}/grants/").status_code == 403
    # but staff may
    assert director.get(f"{ACH}{aid}/grants/").status_code == 200


def test_parent_sees_childs_wall(tenant_a, user_in, as_user, as_role):
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    director, _ = as_role(Role.DIRECTOR)
    parent_user = user_in(tenant_a, roles=[Role.PARENT])
    with schema_context(tenant_a.schema_name):
        child = StudentProfileFactory.create()
        GuardianFactory.create(parent=ParentProfileFactory.create(user=parent_user), student=child)
    aid = director.post(ACH, {"name": "Reader", "scope": "global"}, format="json").json()["data"]["id"]
    director.post(f"{ACH}{aid}/grant/", {"student": child.id}, format="json")

    rows = _rows(as_user(tenant_a, parent_user).get(f"{ACH}mine/").json())
    assert len(rows) == 1
    assert rows[0]["achievement_detail"]["name"] == "Reader"


def test_student_sees_only_active_and_cannot_create(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    teacher_client, _t = as_role(Role.TEACHER)
    student_client, _s = as_role(Role.STUDENT)

    director.post(ACH, {"name": "Active", "scope": "global"}, format="json")
    teacher_client.post(ACH, {"name": "Pending", "scope": "global"}, format="json")  # stays pending

    statuses = {r["status"] for r in _rows(student_client.get(ACH).json())}
    assert statuses == {"active"}  # no pending visible to a student
    assert student_client.post(ACH, {"name": "x", "scope": "global"}, format="json").status_code == 403


def test_role_without_achievements_is_denied(tenant_a, as_role):
    cashier_client, _ = as_role(Role.CASHIER)  # cashier holds no achievements permission
    assert cashier_client.get(ACH).status_code == 403


def test_whitespace_only_name_is_rejected(tenant_a, as_role):
    """A blank/whitespace name must be a 400 (mirrors the old serializer's
    trim_whitespace/allow_blank=False), not a 201 with a junk name stored."""
    director, _ = as_role(Role.DIRECTOR)
    r = director.post(ACH, {"name": "   ", "scope": "global"}, format="json")
    assert r.status_code == 400
    assert "name" in r.json()["errors"]


def test_detail_scope_head_and_multi_codepoint_emoji(tenant_a, user_in, as_user, as_role):
    """Detail reads must obey branch scope, and every safe read route supports HEAD."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create()
        branch_b = BranchFactory.create()
        cohort_a = CohortFactory.create(branch=branch_a)

    teacher_a = _teacher_in_branch(tenant_a, user_in, as_user, branch_a)
    teacher_b = _teacher_in_branch(tenant_a, user_in, as_user, branch_b)
    emoji = "👩\u200d🏫"
    response = teacher_a.post(
        ACH,
        {"name": "Video mentor", "scope": "group", "cohort": cohort_a.id, "emoji": emoji},
        format="json",
    )
    assert response.status_code == 201, response.content
    achievement_id = response.json()["data"]["id"]
    assert response.json()["data"]["emoji"] == emoji

    detail_url = f"{ACH}{achievement_id}/"
    assert teacher_a.get(detail_url).status_code == 200
    assert teacher_b.get(detail_url).status_code == 404
    assert director.head(ACH).status_code == 200
    assert teacher_a.head(detail_url).status_code == 200
    assert director.head(f"{ACH}{achievement_id}/grants/").status_code == 200
    assert teacher_a.head(f"{ACH}mine/").status_code == 200


def test_exact_teacher_can_only_create_list_and_grant_inside_taught_groups(
    tenant_a, user_in, client_for, as_role
):
    """A branch-wide teacher permission must never become branch-wide classroom access."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from tests.role_principal_helpers import exact_session_client

    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
        teacher_profile = TeacherProfileFactory.create(user=teacher_user, branch=branch)
        other_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
        other_profile = TeacherProfileFactory.create(user=other_user, branch=branch)
        mine = CohortFactory.create(branch=branch, primary_teacher=teacher_profile)
        theirs = CohortFactory.create(branch=branch, primary_teacher=other_profile)
        my_student = StudentProfileFactory.create(branch=branch, current_cohort=mine)
        other_student = StudentProfileFactory.create(branch=branch, current_cohort=theirs)

    teacher = exact_session_client(
        client_for,
        tenant_a,
        teacher_user,
        principal_kind="teacher",
        principal_id=teacher_profile.pk,
    )
    other = exact_session_client(
        client_for,
        tenant_a,
        other_user,
        principal_kind="teacher",
        principal_id=other_profile.pk,
    )

    own = teacher.post(ACH, {"name": "My group star", "scope": "group", "cohort": mine.pk}, format="json")
    assert own.status_code == 201, own.content
    blocked = teacher.post(ACH, {"name": "Wrong group", "scope": "group", "cohort": theirs.pk}, format="json")
    assert blocked.status_code == 400
    assert blocked.json()["errors"] == {"cohort": ["Not found."]}

    other_achievement = other.post(
        ACH, {"name": "Other group star", "scope": "group", "cohort": theirs.pk}, format="json"
    )
    assert other_achievement.status_code == 201, other_achievement.content
    visible_ids = {row["id"] for row in _rows(teacher.get(ACH).json())}
    assert own.json()["data"]["id"] in visible_ids
    assert other_achievement.json()["data"]["id"] not in visible_ids

    global_id = director.post(ACH, {"name": "Centre star", "scope": "global"}, format="json").json()["data"][
        "id"
    ]
    assert (
        teacher.post(f"{ACH}{global_id}/grant/", {"student": my_student.pk}, format="json").status_code == 201
    )
    denied = teacher.post(f"{ACH}{global_id}/grant/", {"student": other_student.pk}, format="json")
    assert denied.status_code == 404
    assert denied.json()["code"] == "not_found"
