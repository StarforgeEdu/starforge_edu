"""Adversarial attendance action-scope regressions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.exceptions import PermissionException

pytestmark = pytest.mark.django_db


def test_teacher_cannot_borrow_attendance_write_from_another_branch(tenant_a, as_user):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.attendance.models import AttendanceRecord
    from apps.attendance.services import mark_attendance
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        write_branch = BranchFactory()
        lesson_branch = BranchFactory()
        attendance_writer = AccountType.objects.create(
            name="Scoped attendance marker",
            slug="scoped-attendance-marker",
            account_kind=AccountType.AccountKind.TEACHER,
        )
        AccountTypePermission.objects.create(
            account_type=attendance_writer,
            permission="attendance:write",
        )
        schedule_reader = AccountType.objects.create(
            name="Remote schedule reader",
            slug="remote-schedule-reader",
            account_kind=AccountType.AccountKind.TEACHER,
        )
        AccountTypePermission.objects.create(
            account_type=schedule_reader,
            permission="schedule:read",
        )
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=write_branch,
            account_type=attendance_writer,
            role=attendance_writer.compatibility_role,
        )
        RoleMembership.objects.create(
            user=actor,
            branch=lesson_branch,
            account_type=schedule_reader,
            role=schedule_reader.compatibility_role,
        )
        teacher = TeacherProfileFactory(user=actor, branch=lesson_branch)
        cohort = CohortFactory(branch=lesson_branch, primary_teacher=teacher)
        starts_at = timezone.now() - timedelta(hours=1)
        lesson = Lesson.objects.create(
            term=TermFactory(),
            cohort=cohort,
            teacher=teacher,
            title="Remote lesson",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
        )
        student = StudentProfileFactory(branch=lesson_branch, current_cohort=cohort)
        CohortMembershipFactory(cohort=cohort, student=student)
        actor.refresh_from_db()

        with pytest.raises(PermissionException) as exc:
            mark_attendance(
                lesson=lesson,
                entries=[{"student": student, "status": AttendanceRecord.Status.PRESENT}],
                actor=actor,
            )
        assert exc.value.code == "not_lesson_teacher"

    response = as_user(tenant_a, actor).post(
        f"/api/v1/attendance/lessons/{lesson.pk}/mark/",
        [{"student": student.pk, "status": AttendanceRecord.Status.PRESENT}],
        format="json",
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    with schema_context(tenant_a.schema_name):
        assert not AttendanceRecord.objects.filter(lesson=lesson, student=student).exists()


def test_unknown_and_out_of_roster_student_ids_share_one_failure_contract(
    tenant_a,
    user_in,
    as_user,
):
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from core.permissions import Role

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    actor = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    with schema_context(tenant_a.schema_name):
        teacher = TeacherProfileFactory(user=actor, branch=branch)
        cohort = CohortFactory(branch=branch, primary_teacher=teacher)
        starts_at = timezone.now() - timedelta(hours=1)
        lesson = Lesson.objects.create(
            term=TermFactory(),
            cohort=cohort,
            teacher=teacher,
            title="Scoped roster",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
        )
        outsider = StudentProfileFactory(branch=branch)
        unknown_id = (StudentProfile.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 1000

    client = as_user(tenant_a, actor)

    def mark(student_id):
        return client.post(
            f"/api/v1/attendance/lessons/{lesson.pk}/mark/",
            [{"student": student_id, "status": AttendanceRecord.Status.PRESENT}],
            format="json",
        )

    existing_outsider = mark(outsider.pk)
    unknown = mark(unknown_id)
    assert existing_outsider.status_code == unknown.status_code == 422
    assert existing_outsider.json()["code"] == unknown.json()["code"] == "student_not_in_cohort"
    assert set(existing_outsider.json()["errors"]) == set(unknown.json()["errors"]) == {"students"}


def test_student_attendance_write_grant_cannot_borrow_own_timetable_relationship(tenant_a, as_user):
    """Only teacher/staff natural paths may resolve an attendance write target."""
    from apps.access.models import AccountType, AccountTypePermission
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        malformed_writer = AccountType.objects.create(
            name="Malformed student attendance writer",
            slug="malformed-student-attendance-writer",
            account_kind=AccountType.AccountKind.STUDENT,
        )
        AccountTypePermission.objects.create(
            account_type=malformed_writer,
            permission="attendance:write",
        )
        actor = UserFactory()
        RoleMembership.objects.create(
            user=actor,
            branch=branch,
            account_type=malformed_writer,
            role=malformed_writer.compatibility_role,
        )
        student = StudentProfileFactory(user=actor, branch=branch)
        teacher = TeacherProfileFactory(branch=branch)
        cohort = CohortFactory(branch=branch, primary_teacher=teacher)
        CohortMembershipFactory(cohort=cohort, student=student)
        starts_at = timezone.now() - timedelta(hours=1)
        lesson = Lesson.objects.create(
            term=TermFactory(),
            cohort=cohort,
            teacher=teacher,
            title="Student timetable lesson",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
        )
        actor.refresh_from_db()

    response = as_user(tenant_a, actor).post(
        f"/api/v1/attendance/lessons/{lesson.pk}/mark/",
        [{"student": student.pk, "status": AttendanceRecord.Status.PRESENT}],
        format="json",
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
