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


def _draft(tenant) -> LessonFile:
    """A CLEAN-but-unapproved (tenant-visible) file awaiting publication."""
    with schema_context(tenant.schema_name):
        return LessonFileFactory(is_approved_teacher=False, is_approved_manager=False)


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
    f = _draft(tenant_a)
    teacher_user = user_in(tenant_a, roles=[Role.TEACHER])
    hod_user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT])
    teacher = as_user(tenant_a, teacher_user)
    hod = as_user(tenant_a, hod_user)
    student = as_user(tenant_a, user_in(tenant_a, roles=[Role.STUDENT]))

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
    f = _draft(tenant_a)
    director, _ = as_role(Role.DIRECTOR)
    assert director.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json").status_code == 200
    # the director holds both perms but already signed the teacher leg
    second = director.post(f"{FILES}{f.id}/approve-manager/", {}, format="json")
    assert second.status_code == 403
    assert second.json()["code"] == "dual_control_self"


def test_manager_leg_requires_teacher_first(tenant_a, user_in, as_user):
    f = _draft(tenant_a)
    hod = as_user(tenant_a, user_in(tenant_a, roles=[Role.HEAD_OF_DEPT]))
    r = hod.post(f"{FILES}{f.id}/approve-manager/", {}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "teacher_approval_required"


def test_teacher_cannot_give_manager_approval(tenant_a, user_in, as_user):
    """A teacher holds content:* (so the perm gate passes) but is not a manager —
    the service role gate blocks the elevated second leg."""
    f = _draft(tenant_a)
    t1 = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER]))
    t2 = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER]))
    assert t1.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json").status_code == 200
    blocked = t2.post(f"{FILES}{f.id}/approve-manager/", {}, format="json")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "not_a_manager"


def test_double_teacher_approval_conflicts(tenant_a, user_in, as_user):
    f = _draft(tenant_a)
    teacher = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER]))
    assert teacher.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json").status_code == 200
    again = teacher.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json")
    assert again.status_code == 409
    assert again.json()["code"] == "teacher_already_approved"


def test_student_cannot_approve(tenant_a, user_in, as_user):
    f = _draft(tenant_a)
    student = as_user(tenant_a, user_in(tenant_a, roles=[Role.STUDENT]))
    # students hold neither content:approve nor content:publish
    assert student.post(f"{FILES}{f.id}/approve-teacher/", {}, format="json").status_code == 403
    assert student.post(f"{FILES}{f.id}/approve-manager/", {}, format="json").status_code == 403


def test_view_only_blocks_learner_download(tenant_a, user_in, as_user, monkeypatch):
    from apps.content import services

    monkeypatch.setattr(services, "presign_download", lambda key, **kw: f"https://get/{key}")
    f = _draft(tenant_a)
    t1 = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER]))
    hod_user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT])
    hod = as_user(tenant_a, hod_user)
    student = as_user(tenant_a, user_in(tenant_a, roles=[Role.STUDENT]))

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


def test_manager_reaches_pending_but_not_published_outside_scope(tenant_a, user_in):
    """Least privilege: a HOD reaches a file PENDING the manager sign-off even in
    a library they can't see (to counter-sign it), but gets NO blanket read of
    already-published content in libraries outside their scope."""
    from apps.content import selectors
    from apps.content.tests.factories import ContentLibraryFactory, FolderFactory

    hod = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT])
    with schema_context(tenant_a.schema_name):
        # a ROLE-visibility library only students can see -> outside the HOD's scope
        walled = ContentLibraryFactory(visibility="role", allowed_roles=["student"])
        folder = FolderFactory(library=walled)
        pending = LessonFileFactory(folder=folder, is_approved_teacher=True, is_approved_manager=False)
        published = LessonFileFactory(folder=folder)  # factory -> dual-approved
        reachable = set(
            selectors.scoped_files(user=hod, roles={Role.HEAD_OF_DEPT}).values_list("id", flat=True)
        )
    assert pending.id in reachable  # can counter-sign anything still pending
    assert published.id not in reachable  # but not browse finished walled-off content


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
