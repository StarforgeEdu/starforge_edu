"""Branch / Department / org write services."""

from __future__ import annotations

from typing import Any

from django.apps import apps as django_apps
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.org.models import Branch, BranchTransfer, BranchWorkingHours, Department, StaffProfile
from apps.users.models import RoleMembership
from apps.users.services import create_role_user_bridge, prepare_role_identity
from core.exceptions import ConflictException, ValidationException
from core.permissions import Role

# Enrollment states that still occupy capacity (mirrors Lane D's StudentProfile).
ACTIVE_STUDENT_STATUSES_EXCLUDED = ("graduated", "withdrawn")
STAFF_ROLES = tuple(role for role in Role.ALL if role not in {Role.STUDENT, Role.TEACHER, Role.PARENT})


@transaction.atomic
def create_staff_account(
    *,
    branch: Branch,
    role: str,
    department: Department | None = None,
    username: str = "",
    phone: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    middle_name: str = "",
    birthdate=None,
    gender: str = "",
) -> StaffProfile:
    """Create an independent staff account plus its initial scoped role grant."""
    if role not in STAFF_ROLES:
        raise ValidationException(
            _("Invalid staff role."),
            code="validation_error",
            fields={"role": ["Choose a staff role."]},
        )
    if department is not None and department.branch_id != branch.pk:
        raise ValidationException(
            _("Department must belong to the selected branch."),
            code="department_branch_mismatch",
        )
    identity = prepare_role_identity(
        phone=phone,
        email=email,
        first_name=first_name,
        last_name=last_name,
        middle_name=middle_name,
    )
    if (identity["phone"] and StaffProfile.objects.filter(phone=identity["phone"]).exists()) or (
        identity["email"] and StaffProfile.objects.filter(email__iexact=identity["email"]).exists()
    ):
        raise ValidationException(_("This person already has a staff account."), code="duplicate_staff")
    user, username, identity = create_role_user_bridge(username=username, **identity)
    staff = StaffProfile.objects.create(
        user=user,
        username=username,
        password=user.password,
        first_name=identity["first_name"],
        last_name=identity["last_name"],
        middle_name=identity["middle_name"],
        phone=identity["phone"],
        email=identity["email"],
        birthdate=birthdate,
        gender=gender,
    )
    RoleMembership.objects.create(
        user=user,
        branch=branch,
        department=department,
        role=role,
    )
    return staff


@transaction.atomic
def deactivate_staff_account(staff: StaffProfile) -> None:
    """Disable login and revoke grants/sessions without destroying audit history."""
    now = timezone.now()
    staff.is_active = False
    staff.save(update_fields=["is_active", "updated_at"])
    staff.user.is_active = False
    staff.user.save(update_fields=["is_active"])
    RoleMembership.objects.filter(user=staff.user, revoked_at__isnull=True).update(revoked_at=now)
    from core.session_auth import revoke_all_for_user

    revoke_all_for_user(staff.user_id)


def _teacher_profile_model():
    try:
        return django_apps.get_model("teachers", "TeacherProfile")
    except LookupError:  # Lane D hasn't landed yet — validation no-ops.
        return None


def _student_profile_model():
    try:
        return django_apps.get_model("students", "StudentProfile")
    except LookupError:
        return None


def validate_department_head(teacher, *, branch_id: int | None = None) -> None:
    """Raise unless ``teacher`` is a TeacherProfile in the department branch.

    Single source of truth for D1-LF-4 / D1-LD-10 — shared by the service and
    DepartmentSerializer.validate_head. Once `teachers.TeacherProfile` exists
    the user must have one; until then the check is skipped."""
    if teacher is None:
        return
    TeacherProfile = _teacher_profile_model()
    if TeacherProfile is not None:
        if not isinstance(teacher, TeacherProfile):
            raise ValidationException(_("Department head must be a teacher."), code="head_not_teacher")
        if branch_id is not None and teacher.branch_id != branch_id:
            raise ValidationException(
                _("Department head must teach at the department's branch."),
                code="head_branch_mismatch",
                fields={"head": ["Teacher belongs to a different branch."]},
            )


def set_department_head(department: Department, teacher) -> Department:
    """Assign a department head (validated: head must be a teacher)."""
    validate_department_head(teacher, branch_id=department.branch_id)
    department.head = teacher.user if teacher is not None else None
    department.save(update_fields=["head", "updated_at"])
    return department


def validate_student_id_pattern(pattern: str, *, center_code: str = "") -> None:
    """Guard `CenterSettings.student_id_pattern` (D1-LD-4): it must contain
    {NNNNN} (otherwise generated IDs collide → IntegrityError 500) and a
    rendered sample must fit the 32-char `student_id` column. {YYYY} is
    recommended so the per-year counter reset never collides; its absence is
    not an error (the counter is year-scoped but historic IDs may overlap)."""
    if "{NNNNN}" not in pattern:
        raise ValidationException(
            _("student_id_pattern must contain the {NNNNN} counter placeholder."),
            code="invalid_id_pattern",
        )
    sample = (
        pattern.replace("{CODE}", center_code or "X" * 16)
        .replace("{YYYY}", "2026")
        .replace("{NNNNN}", "00000")
    )
    if len(sample) > 32:  # StudentProfile.student_id max_length
        raise ValidationException(
            _("student_id_pattern renders longer than 32 characters."),
            code="invalid_id_pattern",
        )


@transaction.atomic
def replace_working_hours(branch: Branch, rows: list[dict[str, Any]]) -> list[BranchWorkingHours]:
    """Replace a branch's weekday rows wholesale (D1-LF-2). Validates that open
    times precede close times on non-closed days and that no weekday repeats."""
    weekdays = [row["weekday"] for row in rows]
    if len(weekdays) != len(set(weekdays)):
        raise ValidationException(_("Each weekday may appear at most once."), code="invalid_working_hours")
    for row in rows:
        if not row.get("is_closed", False) and row["opens_at"] >= row["closes_at"]:
            raise ValidationException(_("opens_at must be before closes_at."), code="invalid_working_hours")
    BranchWorkingHours.objects.filter(branch=branch).delete()
    BranchWorkingHours.objects.bulk_create(
        [
            BranchWorkingHours(
                branch=branch,
                weekday=row["weekday"],
                opens_at=row["opens_at"],
                closes_at=row["closes_at"],
                is_closed=row.get("is_closed", False),
            )
            for row in rows
        ]
    )
    return list(BranchWorkingHours.objects.filter(branch=branch).order_by("weekday"))


def archive_branch(branch: Branch) -> Branch:
    """Soft-delete a branch (D1-LF-7). Refuses while it still has active
    students (no-op until Lane D's StudentProfile exists)."""
    StudentProfile = _student_profile_model()
    if StudentProfile is not None:
        has_active = (
            StudentProfile.objects.filter(branch=branch)
            .exclude(status__in=ACTIVE_STUDENT_STATUSES_EXCLUDED)
            .exists()
        )
        if has_active:
            raise ConflictException(_("Branch still has active students."), code="branch_has_active_students")
    branch.archived_at = timezone.now()
    branch.is_active = False
    branch.save(update_fields=["archived_at", "is_active", "updated_at"])
    return branch


def record_transfer(
    *,
    user,
    from_branch: Branch,
    to_branch: Branch,
    reason: str = "",
    actor=None,
) -> BranchTransfer:
    """Record a branch transfer (history only; the cascade is a Day-2 concern)."""
    return BranchTransfer.objects.create(
        user=user, from_branch=from_branch, to_branch=to_branch, reason=reason, actor=actor
    )
