"""StudentService — IStudentService impl.

Repo-injected orchestration that reuses the tested domain functions
(create_student / transition_enrollment / block_student / unblock_student /
import_students_csv) and the role-scoped read selectors, so the enrollment state
machine, generated IDs, paywall, and CSV import semantics are unchanged.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.students.dto.student_dto import StudentCreateDTO, TransitionDTO
from apps.students.interfaces.repositories import (
    IEnrollmentReasonRepository,
    IStudentRepository,
)
from apps.students.interfaces.student_service import (
    IEnrollmentReasonService,
    IStudentService,
)
from apps.students.models import EnrollmentEvent, EnrollmentReason, StudentProfile
from core.exceptions import NotFoundException, ValidationException


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException(_("Invalid input."), code="validation_error", fields={field: [message]})


class EnrollmentReasonService(IEnrollmentReasonService):
    def __init__(self, reasons: IEnrollmentReasonRepository) -> None:
        self._reasons = reasons

    def list_reasons(self) -> QuerySet[EnrollmentReason]:
        return self._reasons.list_reasons()

    def get(self, *, pk: int) -> EnrollmentReason | None:
        return self._reasons.get(pk=pk)

    def create(self, *, data: dict[str, Any]) -> EnrollmentReason:
        data = dict(data)
        if not data.get("slug"):
            data["slug"] = slugify(data.get("name", ""))[:64]
        if not data["slug"]:
            raise _reject("slug", "Could not derive a slug; provide one explicitly.")
        if self._reasons.slug_taken(slug=data["slug"]):
            raise _reject("slug", "An enrollment reason with this slug already exists.")
        return self._reasons.add(data=data)

    def update(self, reason: EnrollmentReason, *, changes: dict[str, Any]) -> EnrollmentReason:
        if "slug" in changes and self._reasons.slug_taken(slug=changes["slug"], exclude_pk=reason.pk):
            raise _reject("slug", "An enrollment reason with this slug already exists.")
        return self._reasons.apply_changes(reason, changes=changes)

    def delete(self, reason: EnrollmentReason) -> None:
        self._reasons.remove(reason)

    def active_slugs(self) -> set[str]:
        return self._reasons.active_slugs()


# Direct-edit fields only (StudentUpdateSerializer): current_cohort/branch/status
# are deliberately NOT here — those change via the cohort move / transfer / transition
# services so history + signals + capacity checks stay intact.
_UPDATABLE = ("academic_level", "location", "previous_school", "medical_notes", "emergency_contacts")
_IDENTITY_FIELDS = (
    "first_name",
    "last_name",
    "middle_name",
    "phone",
    "email",
    "birthdate",
    "gender",
    "is_active",
)


class StudentService(IStudentService):
    def __init__(self, students: IStudentRepository) -> None:
        self._students = students

    # --- CRUD --------------------------------------------------------------
    def scoped_list(self, *, user, roles) -> QuerySet[StudentProfile]:
        return self._students.scoped(user=user, roles=roles)

    def get(self, *, user, roles, pk: int) -> StudentProfile | None:
        return self._students.get_scoped(user=user, roles=roles, pk=pk)

    def create(self, data: StudentCreateDTO) -> StudentProfile:
        from apps.students.services import create_student

        return create_student(
            branch=self._resolve_active_branch(data.branch_id),
            username=data.username,
            phone=data.phone,
            email=data.email,
            first_name=data.first_name,
            last_name=data.last_name,
            middle_name=data.middle_name,
            birthdate=data.birthdate,
            gender=data.gender,
            status=data.status,
            academic_level=data.academic_level,
            location=data.location,
            previous_school=data.previous_school,
            medical_notes=data.medical_notes,
            emergency_contacts=data.emergency_contacts,
        )

    def update(self, student: StudentProfile, changes: dict[str, Any]) -> StudentProfile:
        identity_changes = {field: changes[field] for field in _IDENTITY_FIELDS if field in changes}
        if identity_changes:
            from apps.users.services import update_role_identity

            update_role_identity(student, identity_changes)
        for field in _UPDATABLE:
            if field in changes:
                setattr(student, field, changes[field])
        if any(field in changes for field in _UPDATABLE):
            student.save()
        return student

    def delete(self, student: StudentProfile) -> None:
        self._students.delete(student)

    # --- detail actions ----------------------------------------------------
    def transition(self, student: StudentProfile, data: TransitionDTO, actor) -> StudentProfile:
        from apps.students.services import transition_enrollment

        return transition_enrollment(
            student=student,
            to_status=data.to_status,
            reason_code=data.reason_code,
            note=data.note,
            actor=actor,
        )

    def block(self, student: StudentProfile, reason: str, actor) -> StudentProfile:
        from apps.students.services import block_student

        return block_student(student=student, reason=reason, actor=actor)

    def unblock(self, student: StudentProfile, actor) -> StudentProfile:
        from apps.students.services import unblock_student

        return unblock_student(student=student, actor=actor)

    def events(self, student: StudentProfile) -> QuerySet[EnrollmentEvent]:
        return student.enrollment_events.all()

    def issue_credentials(self, student: StudentProfile, *, actor) -> dict[str, Any]:
        """Issue a ONE-TIME login password for the student so they can sign in at
        /role-login/ (accounts are created passwordless). Generates a temp password, sets
        it on the student account, flags the account must-change (so the client forces a
        reset on first login), ends any existing session, and returns
        {username, temporary_password} — the temp is never stored/echoed again."""
        from apps.users.services import issue_role_credentials

        return issue_role_credentials(
            student,
            actor=actor,
            resource_type="students.StudentProfile",
        )

    # --- collection actions ------------------------------------------------
    def import_csv(self, *, file_obj, branch_id: int) -> dict[str, Any]:
        from apps.students.services import import_students_csv

        return import_students_csv(file_obj=file_obj, branch=self._resolve_active_branch(branch_id))

    def birthdays(self, *, user, roles, days: int, branch, cohort) -> QuerySet[StudentProfile]:
        from apps.students.selectors import students_with_upcoming_birthdays

        return students_with_upcoming_birthdays(
            base=self.scoped_list(user=user, roles=roles), days=days, branch=branch, cohort=cohort
        )

    def stats(self, *, user, roles) -> dict[str, Any]:
        from apps.students.selectors import student_stats

        return student_stats(self.scoped_list(user=user, roles=roles))

    def comparison(self, *, user, roles, metric: str, unit: str) -> dict[str, Any]:
        from apps.students.selectors import student_comparison

        return student_comparison(self.scoped_list(user=user, roles=roles), metric=metric, unit=unit)

    # --- self-service ------------------------------------------------------
    def require_profile(self, user) -> StudentProfile:
        student = self._students.profile_for(user)
        if student is None:
            raise NotFoundException(_("You do not have a student profile."), code="not_a_student")
        return student

    def dashboard(self, *, user, roles) -> dict[str, Any]:
        from apps.students.selectors import student_dashboard

        return student_dashboard(student=self.require_profile(user), user=user, roles=roles)

    def report(self, *, user) -> dict[str, Any]:
        from apps.students.selectors import student_report

        return student_report(student=self.require_profile(user))

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _resolve_active_branch(branch_id: int):
        from apps.org.models import Branch

        # Archived branches are not assignable (D1-LF-7) — mirrors _active_branches().
        branch = Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
        if branch is None:
            raise ValidationException(
                _("Invalid branch."), code="invalid_branch", fields={"branch": ["Not found."]}
            )
        return branch
