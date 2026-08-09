"""F4-5 — dual publication approval + view-only toggle.

A CLEAN file reaches learners only after BOTH a teacher and a manager sign off
(maker-checker: two different people; the second leg needs a manager role). A
manager may publish a file as view-only so learners can stream but not download
it. Real uploads start unapproved; the test factory ships pre-published files.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.content.models import LessonFile
from apps.content.tests.factories import LessonFileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

FILES = "/api/v1/content/files/"


def _draft(tenant):
    """A CLEAN, unapproved file inside one explicit branch/cohort scope."""
    from apps.cohorts.tests.factories import CohortFactory
    from apps.content.tests.factories import ContentLibraryFactory, FolderFactory
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        library = ContentLibraryFactory(visibility="cohort", cohort=cohort)
        file = LessonFileFactory(
            folder=FolderFactory(library=library),
            is_approved_teacher=False,
            is_approved_manager=False,
        )
        return file, branch, cohort


def _learner_for(tenant, *, branch, cohort, user_in, as_user):
    from apps.cohorts.tests.factories import CohortMembershipFactory
    from apps.students.tests.factories import StudentProfileFactory

    user = user_in(tenant, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant.schema_name):
        student = StudentProfileFactory(user=user, branch=branch, current_cohort=cohort)
        CohortMembershipFactory(cohort=cohort, student=student)
    return as_user(tenant, user)


def test_real_upload_starts_unapproved_and_downloadable():
    """The model defaults a fresh file to unapproved (must run the workflow)."""
    f = LessonFile(title="x", s3_key="k", content_type="application/pdf", size_bytes=1)
    assert f.is_approved_teacher is False
    assert f.is_approved_manager is False
    assert f.is_downloadable is True


def test_learner_sees_only_dual_approved_files(tenant_a, user_in):
    from apps.content import selectors

    student = user_in(tenant_a, roles=[Role.STUDENT])
    with schema_context(tenant_a.schema_name):
        published = LessonFileFactory()  # factory -> dual-approved
        draft = LessonFileFactory(is_approved_teacher=True, is_approved_manager=False)
        visible = set(selectors.scoped_files(user=student, roles={Role.STUDENT}).values_list("id", flat=True))
    assert published.id in visible
    assert draft.id not in visible  # half-approved is still hidden from learners


def test_full_publication_flow(tenant_a, user_in, as_user):
    f, branch, cohort = _draft(tenant_a)
    teacher_user = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    hod_user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    teacher = as_user(tenant_a, teacher_user)
    hod = as_user(tenant_a, hod_user)
    student = _learner_for(
        tenant_a,
        branch=branch,
        cohort=cohort,
        user_in=user_in,
        as_user=as_user,
    )

    # teacher signs the first leg
    r1 = teacher.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json")
    assert r1.status_code == 200, r1.content
    assert r1.json()["data"]["is_approved_teacher"] is True
    assert r1.json()["data"]["is_approved_manager"] is False
    # the maker-checker trail surfaces WHO signed the teacher leg
    assert r1.json()["data"]["approved_teacher_by"] == teacher_user.id
    # half-approved: the learner still can't see it
    assert student.get(FILES).json()["pagination"]["total"] == 0

    # a manager counter-signs -> published
    r2 = hod.post(f"{FILES}{f.id}/approve-manager/", {}, format="json")
    assert r2.status_code == 200, r2.content
    assert r2.json()["data"]["is_approved_manager"] is True
    assert r2.json()["data"]["approved_manager_by"] == hod_user.id  # second signer surfaced too
    # now the learner sees it
    assert [row["id"] for row in student.get(FILES).json()["data"]] == [f.id]


def test_manager_leg_requires_a_different_person(tenant_a, as_role):
    """Maker-checker: the same person cannot sign both legs."""
    f, _branch, _cohort = _draft(tenant_a)
    director, _ = as_role(Role.DIRECTOR)
    assert director.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json").status_code == 200
    # the director holds both perms but already signed the teacher leg
    second = director.post(f"{FILES}{f.id}/approve-manager/", {}, format="json")
    assert second.status_code == 403
    assert second.json()["code"] == "dual_control_self"


def test_manager_leg_requires_teacher_first(tenant_a, user_in, as_user):
    f, branch, _cohort = _draft(tenant_a)
    hod = as_user(
        tenant_a,
        user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch),
    )
    r = hod.post(f"{FILES}{f.id}/approve-manager/", {}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "teacher_approval_required"


def test_teacher_cannot_give_manager_approval(tenant_a, user_in, as_user):
    """A teacher holds content:* (so the perm gate passes) but is not a manager —
    the service role gate blocks the elevated second leg."""
    f, branch, _cohort = _draft(tenant_a)
    t1 = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    t2 = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    assert t1.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json").status_code == 200
    blocked = t2.post(f"{FILES}{f.id}/approve-manager/", {}, format="json")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "not_a_manager"


def test_custom_reviewer_and_publisher_types_are_permission_scoped(
    tenant_a,
    user_in,
    as_user,
):
    """Custom STAFF types must not be reduced to their SUPPORT compatibility role.

    Explicit approve/publish grants drive both workflow eligibility and the exact
    branch/department row boundary.
    """
    from apps.access.models import AccountType, AccountTypePermission
    from apps.content.tests.factories import ContentLibraryFactory, FolderFactory
    from apps.org.tests.factories import BranchFactory, DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        department = DepartmentFactory(branch=branch)
        other_branch = BranchFactory()
        other_department = DepartmentFactory(branch=other_branch)
        library = ContentLibraryFactory(
            visibility="department",
            department=department,
        )
        other_library = ContentLibraryFactory(
            visibility="department",
            department=other_department,
        )
        pending = LessonFileFactory(
            folder=FolderFactory(library=library),
            is_approved_teacher=False,
            is_approved_manager=False,
        )
        out_of_scope = LessonFileFactory(
            folder=FolderFactory(library=other_library),
            is_approved_teacher=False,
            is_approved_manager=False,
        )
        reviewer_type = AccountType.objects.create(
            name="Content Reviewer",
            slug="content-reviewer",
            account_kind=AccountType.AccountKind.STAFF,
        )
        publisher_type = AccountType.objects.create(
            name="Content Publisher",
            slug="content-publisher",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=reviewer_type,
            permission="content:approve",
        )
        AccountTypePermission.objects.create(
            account_type=publisher_type,
            permission="content:publish",
        )

    reviewer_user = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    publisher_user = user_in(tenant_a, roles=[Role.SUPPORT], branch=branch)
    with schema_context(tenant_a.schema_name):
        reviewer_user.role_memberships.update(
            account_type=reviewer_type,
            department=department,
        )
        publisher_user.role_memberships.update(
            account_type=publisher_type,
            department=department,
        )
        reviewer_user.refresh_from_db()
        publisher_user.refresh_from_db()

    reviewer = as_user(tenant_a, reviewer_user)
    publisher = as_user(tenant_a, publisher_user)
    first = reviewer.post(f"{FILES}{pending.id}/approve-teacher/", {}, format="json")
    assert first.status_code == 200, first.content
    assert reviewer.post(f"{FILES}{out_of_scope.id}/approve-teacher/", {}, format="json").status_code == 404

    second = publisher.post(f"{FILES}{pending.id}/approve-manager/", {}, format="json")
    assert second.status_code == 200, second.content
    assert second.json()["data"]["approved_manager_by"] == publisher_user.id
    assert publisher.post(f"{FILES}{out_of_scope.id}/approve-manager/", {}, format="json").status_code == 404


def test_double_teacher_approval_conflicts(tenant_a, user_in, as_user):
    f, branch, _cohort = _draft(tenant_a)
    teacher = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    assert teacher.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json").status_code == 200
    again = teacher.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json")
    assert again.status_code == 409
    assert again.json()["code"] == "teacher_already_approved"


def test_student_cannot_approve(tenant_a, user_in, as_user):
    f, branch, _cohort = _draft(tenant_a)
    student = as_user(tenant_a, user_in(tenant_a, roles=[Role.STUDENT], branch=branch))
    # students hold neither content:approve nor content:publish
    assert student.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json").status_code == 403
    assert student.post(f"{FILES}{f.id}/approve-manager/", {}, format="json").status_code == 403


def test_view_only_blocks_learner_download(tenant_a, user_in, as_user, monkeypatch):
    from apps.content import services

    monkeypatch.setattr(services, "presign_download", lambda key, **kw: f"https://get/{key}")
    f, branch, cohort = _draft(tenant_a)
    t1 = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    hod_user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    hod = as_user(tenant_a, hod_user)
    student = _learner_for(
        tenant_a,
        branch=branch,
        cohort=cohort,
        user_in=user_in,
        as_user=as_user,
    )

    assert t1.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json").status_code == 200
    # publish as view-only
    pub = hod.post(f"{FILES}{f.id}/approve-manager/", {"is_downloadable": False}, format="json")
    assert pub.status_code == 200
    assert pub.json()["data"]["is_downloadable"] is False

    # the learner may stream (track-view) but gets no download URL
    assert student.post(f"{FILES}{f.id}/track-view/", {}, format="json").status_code == 204
    blocked = student.get(f"{FILES}{f.id}/download-url/")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "file_view_only"

    # content staff may still pull the bytes to manage the file
    assert hod.get(f"{FILES}{f.id}/download-url/").status_code == 200


def test_new_version_resets_publication(tenant_a, user_in, monkeypatch):
    """A new version is a fresh, unapproved row — it must be re-signed before it
    reaches learners again (the previous published version stays visible)."""
    from apps.content import services

    monkeypatch.setattr(services, "presign_upload", lambda key, **kw: f"https://put/{key}")
    uploader = user_in(tenant_a, roles=[Role.TEACHER])
    with schema_context(tenant_a.schema_name):
        published = LessonFileFactory()  # dual-approved
        result = services.create_new_version(
            previous=published,
            filename="next.pdf",
            content_type="application/pdf",
            size_bytes=10,
            user=uploader,
        )
        new_file = result["file"]
    assert new_file.is_approved_teacher is False
    assert new_file.is_approved_manager is False


def test_manager_reaches_pending_only_inside_exact_scope(tenant_a, user_in):
    """A publish grant never lends the manager access outside its membership boundary."""
    from apps.content import selectors
    from apps.content.tests.factories import ContentLibraryFactory, FolderFactory
    from apps.org.tests.factories import DepartmentFactory
    from tests.role_principal_helpers import ensure_role_principal

    hod = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT])
    with schema_context(tenant_a.schema_name):
        branch = hod.role_memberships.get().branch
        ensure_role_principal(
            hod,
            roles=[Role.HEAD_OF_DEPT],
            branch=branch,
        )
        department = DepartmentFactory(branch=branch)
        in_scope = ContentLibraryFactory(visibility="department", department=department)
        in_scope_pending = LessonFileFactory(
            folder=FolderFactory(library=in_scope),
            is_approved_teacher=True,
            is_approved_manager=False,
        )
        # a ROLE-visibility library only students can see -> outside the HOD's scope
        walled = ContentLibraryFactory(visibility="role", allowed_roles=["student"])
        folder = FolderFactory(library=walled)
        pending = LessonFileFactory(folder=folder, is_approved_teacher=True, is_approved_manager=False)
        published = LessonFileFactory(folder=folder)  # factory -> dual-approved
        reachable = set(selectors.scoped_files(user=hod).values_list("id", flat=True))
    assert in_scope_pending.id in reachable
    assert pending.id not in reachable
    assert published.id not in reachable


@pytest.mark.django_db(transaction=True)
def test_publish_works_under_real_autocommit(tenant_a, user_in):
    """The approval legs use select_for_update, which needs an explicit
    @transaction.atomic — exercise the REAL autocommit path (no ambient test
    transaction) so a missing decorator would surface as a 500, not pass silently."""
    from apps.content import services

    teacher = user_in(tenant_a, roles=[Role.TEACHER])
    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT])
    with schema_context(tenant_a.schema_name):
        f = LessonFileFactory(is_approved_teacher=False, is_approved_manager=False)
        services.approve_teacher_leg(file=f, actor=teacher)
        published = services.approve_manager_leg(file=f, actor=manager, actor_roles={Role.HEAD_OF_DEPT})
    assert published.is_approved_teacher is True
    assert published.is_approved_manager is True
