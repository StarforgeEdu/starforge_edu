"""StudentService — IStudentService impl.

Repo-injected orchestration that reuses the tested domain functions
(create_student / transition_enrollment / block_student / unblock_student /
import_students_csv) and the role-scoped read selectors, so the enrollment state
machine, generated IDs, paywall, and CSV import semantics are unchanged.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.students.dto.student_dto import (
    LeadershipProfileAccessDTO,
    LeadershipProfileWindowDTO,
    StudentCreateDTO,
    TransitionDTO,
)
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
        # Reasons are denormalized into historical EnrollmentEvent rows. Retain
        # the catalogue row/name and retire it from future transitions.
        self._reasons.apply_changes(reason, changes={"is_active": False})

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

    @transaction.atomic
    def update(self, student: StudentProfile, changes: dict[str, Any]) -> StudentProfile:
        from apps.users.models import User
        from core.identity_lifecycle import assert_exclusive_role_bridge

        unsupported = sorted(set(changes) - set(_IDENTITY_FIELDS) - set(_UPDATABLE))
        if unsupported:
            raise ValidationException(
                _("Unsupported student-account field."),
                code="validation_error",
                fields={field: [_("This field is not supported.")] for field in unsupported},
            )
        student = (
            StudentProfile.objects.select_for_update(of=("self",))
            .select_related("user", "branch", "current_cohort")
            .defer("medical_notes", "emergency_contacts")
            .get(pk=student.pk)
        )
        identity_changes = {field: changes[field] for field in _IDENTITY_FIELDS if field in changes}
        if identity_changes:
            # The bridge stores the authorization graph. A legacy bridge shared
            # by two role profiles cannot be renamed or deactivated as one role.
            User.objects.select_for_update().get(pk=student.user_id)
            assert_exclusive_role_bridge(student, principal_kind="student")
            from apps.users.services import prepare_role_identity, update_role_identity

            if {"phone", "email"} & identity_changes.keys():
                normalized = prepare_role_identity(
                    phone=identity_changes.get("phone", student.phone),
                    email=identity_changes.get("email", student.email),
                    first_name=identity_changes.get("first_name", student.first_name),
                    last_name=identity_changes.get("last_name", student.last_name),
                    middle_name=identity_changes.get("middle_name", student.middle_name),
                )
                if not normalized["phone"] and not normalized["email"]:
                    raise ValidationException(
                        _("Keep a phone number or email on the student account."),
                        code="identifier_required",
                        fields={
                            "phone": [_("A phone or email is required.")],
                            "email": [_("A phone or email is required.")],
                        },
                    )
            try:
                update_role_identity(student, identity_changes)
            except IntegrityError as exc:
                conflicting_fields = sorted({"phone", "email"} & identity_changes.keys())
                if not conflicting_fields:
                    raise
                raise ValidationException(
                    _("This contact already belongs to another student account."),
                    code="duplicate_account",
                    fields={field: [_("Choose a unique contact value.")] for field in conflicting_fields},
                ) from exc
        for field in _UPDATABLE:
            if field in changes:
                setattr(student, field, changes[field])
        if any(field in changes for field in _UPDATABLE):
            student.save()
        return student

    @transaction.atomic
    def deactivate(self, student: StudentProfile, *, actor) -> StudentProfile:
        """Disable login/authorization while retaining the authoritative record.

        Hard deletion would cascade enrollment, family, placement, cohort, and
        award history. The database separately rejects direct hard deletes.
        """
        from apps.audit.scopes import scoped_audit_scope
        from apps.audit.services import audit_log
        from apps.users.models import User
        from apps.users.services import revoke_role_account_access
        from core.identity_lifecycle import assert_exclusive_role_bridge

        student = (
            StudentProfile.objects.select_for_update(of=("self",))
            .select_related("user", "branch", "current_cohort")
            .defer("medical_notes", "emergency_contacts")
            .get(pk=student.pk)
        )
        User.objects.select_for_update().get(pk=student.user_id)
        assert_exclusive_role_bridge(student, principal_kind="student")
        if not student.is_active:
            return student

        revoke_role_account_access(student)
        cohort = student.current_cohort
        audit_log(
            actor=actor,
            action="update",
            resource_type="students.StudentProfile",
            resource_id=student.pk,
            before={"is_active": True, "branch_id": student.branch_id},
            after={"is_active": False, "branch_id": student.branch_id},
            scope=scoped_audit_scope(
                student.branch_id,
                cohort.department_id if cohort is not None else None,
            ),
        )
        return student

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

    @transaction.atomic
    def issue_credentials(self, student: StudentProfile, *, actor) -> dict[str, Any]:
        """Issue a ONE-TIME login password for the student so they can sign in at
        /role-login/ (accounts are created passwordless). Generates a temp password, sets
        it on the student account, flags the account must-change (so the client forces a
        reset on first login), ends any existing session, and returns
        {username, temporary_password} — the temp is never stored/echoed again."""
        from apps.audit.scopes import scoped_audit_scope
        from apps.users.models import User
        from apps.users.services import issue_role_credentials
        from core.exceptions import ConflictException
        from core.identity_lifecycle import assert_exclusive_role_bridge

        student = (
            StudentProfile.objects.select_for_update(of=("self",))
            .select_related("user", "current_cohort")
            .defer("medical_notes", "emergency_contacts")
            .get(pk=student.pk)
        )
        user = User.objects.select_for_update().get(pk=student.user_id)
        assert_exclusive_role_bridge(student, principal_kind="student")
        if not student.is_active or not user.is_active:
            raise ConflictException(
                _("Inactive student accounts cannot receive new credentials."),
                code="account_inactive",
            )

        cohort = student.current_cohort if student.current_cohort_id is not None else None
        return issue_role_credentials(
            student,
            actor=actor,
            resource_type="students.StudentProfile",
            audit_scopes=(
                scoped_audit_scope(
                    student.branch_id,
                    cohort.department_id if cohort is not None else None,
                ),
            ),
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

    def leadership_profile(
        self,
        *,
        student: StudentProfile,
        user,
        roles,
        window: LeadershipProfileWindowDTO,
        access: LeadershipProfileAccessDTO,
    ) -> dict[str, Any]:
        from apps.students.leadership import build_student_leadership_profile

        # The normal directory query intentionally keeps its joins narrow. This
        # aggregate needs readable department/teacher relationships and reloads
        # only the already authorization-proven record in one bounded query.
        hydrated = (
            StudentProfile.objects.select_related(
                "user",
                "branch",
                "current_cohort__department",
                "current_cohort__primary_teacher",
            )
            .defer("medical_notes", "emergency_contacts")
            .get(pk=student.pk)
        )
        return build_student_leadership_profile(
            student=hydrated,
            user=user,
            roles=roles,
            window=window,
            access=access,
        )

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
