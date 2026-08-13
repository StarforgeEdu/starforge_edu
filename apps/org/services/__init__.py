"""Branch / Department / org write services."""

from __future__ import annotations

from typing import Any

from django.apps import apps as django_apps
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.org.models import Branch, BranchTransfer, BranchWorkingHours, Department, StaffProfile
from apps.users.services import create_role_user_bridge, ensure_role_membership, prepare_role_identity
from core.exceptions import (
    ConflictException,
    NotFoundException,
    ValidationException,
)
from core.permissions import Role
from core.role_principals import validate_role_principal

# Enrollment states that still occupy capacity (mirrors Lane D's StudentProfile).
ACTIVE_STUDENT_STATUSES_EXCLUDED = ("graduated", "withdrawn")
STAFF_ROLES = tuple(role for role in Role.ALL if role not in {Role.STUDENT, Role.TEACHER, Role.PARENT})


@transaction.atomic
def create_staff_account(
    *,
    branch: Branch,
    role: str | None = None,
    account_type=None,
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
    if account_type is None and role not in STAFF_ROLES:
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
    ensure_role_membership(
        staff,
        branch=branch,
        department=department,
        role=role,
        account_type=account_type,
    )
    return staff


@transaction.atomic
def deactivate_staff_account(staff: StaffProfile) -> None:
    """Disable login and revoke grants/sessions without destroying audit history."""
    from apps.users.services import revoke_role_account_access

    revoke_role_account_access(staff)


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
    # Serialize replacement calls. Without a stable parent-row lock, two valid
    # delete/bulk-create sequences can interleave and hit a duplicate constraint
    # or leave a mixture of both requests.
    branch = Branch.objects.select_for_update().get(pk=branch.pk)
    weekdays = [row["weekday"] for row in rows]
    if len(weekdays) != len(set(weekdays)):
        raise ValidationException(_("Each weekday may appear at most once."), code="invalid_working_hours")
    for row in rows:
        if not row.get("is_closed", False) and row["opens_at"] >= row["closes_at"]:
            raise ValidationException(_("opens_at must be before closes_at."), code="invalid_working_hours")
    BranchWorkingHours.objects.filter(branch=branch).delete()
    # There are at most seven rows. Save individually so organization-calendar
    # audit receivers capture each new immutable scope snapshot; bulk_create
    # bypasses Django signals and would leave this security-sensitive policy
    # change unattributed for negligible performance benefit.
    for row in rows:
        BranchWorkingHours.objects.create(
            branch=branch,
            weekday=row["weekday"],
            opens_at=row["opens_at"],
            closes_at=row["closes_at"],
            is_closed=row.get("is_closed", False),
        )
    return list(BranchWorkingHours.objects.filter(branch=branch).order_by("weekday"))


@transaction.atomic
def archive_branch(branch: Branch) -> Branch:
    """Soft-delete a branch (D1-LF-7). Refuses while it still has active
    students (no-op until Lane D's StudentProfile exists)."""
    locked_branch = Branch.objects.select_for_update().filter(pk=branch.pk).first()
    if locked_branch is None:
        raise NotFoundException(code="not_found")
    branch = locked_branch
    if branch.archived_at is not None:
        return branch
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
    user=None,
    from_branch: Branch,
    to_branch: Branch,
    reason: str = "",
    actor=None,
    student=None,
    subject_kind: str = "",
    subject_id: int | None = None,
    subject_name: str = "",
    subject_reference: str = "",
    actor_principal_kind: str = "",
    actor_principal_id: int | None = None,
) -> BranchTransfer:
    """Append one branch-transfer audit row inside the caller's transaction."""
    reason = reason.strip()
    if len(reason) > 64:
        raise ValidationException(
            _("Reason is too long."),
            code="validation_error",
            fields={"reason": [_("Must be at most 64 characters.")]},
        )
    if from_branch.pk == to_branch.pk:
        raise ValidationException(_("Transfer branches must differ."), code="same_branch")
    if actor is None:
        actor_is_valid = actor_principal_kind == "" and actor_principal_id is None
    else:
        actor_is_valid = bool(actor_principal_kind) and actor_principal_id is not None
    if not actor_is_valid:
        raise ValidationException(
            _("Actor attribution is invalid."),
            code="validation_error",
        )
    if actor is not None:
        validate_role_principal(
            kind=actor_principal_kind,
            principal_id=actor_principal_id,  # type: ignore[arg-type]  # checked above
            user_id=actor.pk,
            field="actor",
        )
    student_resolved = student is not None and user is not None and student.user_id == user.pk
    if not subject_kind:
        subject_kind = (
            BranchTransfer.SubjectKind.STUDENT if student_resolved else BranchTransfer.SubjectKind.LEGACY
        )
    if subject_kind == BranchTransfer.SubjectKind.STUDENT:
        if not student_resolved:
            raise ValidationException(
                _("Student transfer attribution is invalid."),
                code="validation_error",
            )
        subject_id = student.pk
        subject_name = student.get_full_name()
        subject_reference = student.student_id
    elif subject_kind in (
        BranchTransfer.SubjectKind.TEACHER,
        BranchTransfer.SubjectKind.STAFF,
    ):
        if user is None or subject_id is None or not subject_name.strip():
            raise ValidationException(_("Transfer attribution is invalid."), code="validation_error")
    elif subject_kind == BranchTransfer.SubjectKind.COHORT:
        if user is not None or subject_id is None or not subject_name.strip():
            raise ValidationException(_("Group transfer attribution is invalid."), code="validation_error")
    elif subject_kind != BranchTransfer.SubjectKind.LEGACY:
        raise ValidationException(_("Transfer type is invalid."), code="validation_error")
    actor_name = _principal_display_name(
        actor,
        kind=actor_principal_kind,
        principal_id=actor_principal_id,
    )
    return BranchTransfer.objects.create(
        user=user,
        subject_kind=subject_kind,
        subject_id=subject_id,
        subject_name=subject_name.strip()[:452],
        subject_reference=subject_reference.strip()[:150],
        student=student if student_resolved else None,
        student_public_id=student.student_id if student_resolved else "",
        student_name=student.get_full_name() if student_resolved else "",
        student_attribution_status=(
            BranchTransfer.AttributionStatus.RESOLVED
            if student_resolved
            else BranchTransfer.AttributionStatus.UNRESOLVED
        ),
        from_branch=from_branch,
        to_branch=to_branch,
        reason=reason,
        actor=actor,
        actor_principal_kind=actor_principal_kind if actor_principal_id is not None else "",
        actor_principal_id=actor_principal_id,
        actor_name=actor_name,
    )


def _principal_display_name(actor, *, kind: str, principal_id: int | None) -> str:
    if actor is None or principal_id is None or not kind:
        return ""
    model_labels = {
        "staff": "org.StaffProfile",
        "teacher": "teachers.TeacherProfile",
        "student": "students.StudentProfile",
        "parent": "parents.ParentProfile",
    }
    model_label = model_labels.get(kind)
    if model_label is None:
        return ""
    try:
        model = django_apps.get_model(model_label)
        principal = model.objects.filter(pk=principal_id, user_id=actor.pk).first()
    except (LookupError, TypeError, ValueError):
        return ""
    if principal is None:
        return ""
    get_full_name = getattr(principal, "get_full_name", None)
    display_name = get_full_name() if callable(get_full_name) else ""
    # Keep the snapshot reproducible by the database integrity trigger. A
    # model-specific __str__ may include mutable/internal identifiers; when both
    # name and username are absent, an empty (unknown) display is more truthful.
    return str(display_name or getattr(principal, "username", "") or "")[:452]


@transaction.atomic
def transfer_student(
    *,
    student_id: int,
    to_branch_id: int,
    reason: str = "",
    actor=None,
    actor_principal_kind: str = "",
    actor_principal_id: int | None = None,
    allowed_branch_ids: set[int] | None = None,
) -> BranchTransfer:
    """Move one student between branches without leaving stale scope or cohorts.

    ``allowed_branch_ids=None`` is the explicit director/superuser bypass. Every
    scoped caller must cover both the source and target branch with the exact
    membership that grants ``org:write``. All state and the audit row commit as a
    unit; any validation or downstream failure rolls the move back.
    """
    from apps.access.models import AccountType
    from apps.cohorts.models import CohortMembership
    from apps.students.models import StudentProfile
    from apps.users.models import RoleMembership, User

    reason = reason.strip()
    if len(reason) > 64:
        raise ValidationException(
            _("Reason is too long."),
            code="validation_error",
            fields={"reason": [_("Must be at most 64 characters.")]},
        )
    if allowed_branch_ids is not None and to_branch_id not in allowed_branch_ids:
        raise NotFoundException(code="not_found")

    student = (
        StudentProfile.objects.select_for_update().select_related("branch").filter(pk=student_id).first()
    )
    if student is None:
        raise NotFoundException(_("Student not found."), code="not_found")
    if allowed_branch_ids is not None and student.branch_id not in allowed_branch_ids:
        raise NotFoundException(code="not_found")
    if student.branch_id == to_branch_id:
        raise ValidationException(
            _("Student already belongs to that branch."),
            code="same_branch",
            fields={"to_branch": [_("Choose a different branch.")]},
        )

    # Lock both branch rows in deterministic order. The source is PROTECT-ed by
    # StudentProfile, while the target must be active and unarchived at commit.
    branches = Branch.objects.select_for_update().filter(pk__in=sorted({student.branch_id, to_branch_id}))
    branch_by_id = {branch.pk: branch for branch in branches}
    from_branch = branch_by_id.get(student.branch_id)
    to_branch = branch_by_id.get(to_branch_id)
    if from_branch is None:  # defensive against legacy broken FK constraints
        raise ValidationException(_("Current branch is unavailable."), code="invalid_source_branch")
    if to_branch is None or not to_branch.is_active or to_branch.archived_at is not None:
        raise ValidationException(
            _("Choose an active target branch."),
            code="invalid_target_branch",
            fields={"to_branch": [_("Choose an active branch.")]},
        )

    # Serialize with cohort enroll/move/unenroll and retain membership history.
    active_memberships = list(
        CohortMembership.objects.select_for_update()
        .select_related("cohort")
        .filter(student=student, end_date__isnull=True)
        .order_by("-start_date", "-pk")
    )
    incompatible_ids = [
        membership.pk for membership in active_memberships if membership.cohort.branch_id != to_branch.pk
    ]
    if incompatible_ids:
        CohortMembership.objects.filter(pk__in=incompatible_ids).update(
            end_date=timezone.localdate(),
            moved_reason=reason,
        )
    compatible = [
        membership for membership in active_memberships if membership.cohort.branch_id == to_branch.pk
    ]
    compatible_ids = {membership.cohort_id for membership in compatible}
    if student.current_cohort_id not in compatible_ids:
        student.current_cohort_id = compatible[0].cohort_id if compatible else None

    # Lock the compatibility principal and every grant before changing scope.
    User.objects.select_for_update().get(pk=student.user_id)
    list(RoleMembership.objects.select_for_update().filter(user_id=student.user_id))
    ensure_role_membership(student, branch=to_branch, role=Role.STUDENT)
    _align_student_account_type_scopes(
        student=student,
        to_branch=to_branch,
        account_type_model=AccountType,
        membership_model=RoleMembership,
    )

    student.branch = to_branch
    student.save(update_fields=["branch", "current_cohort", "updated_at"])
    return record_transfer(
        user=student.user,
        from_branch=from_branch,
        to_branch=to_branch,
        reason=reason,
        actor=actor,
        student=student,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )


def _locked_transfer_branches(
    *,
    from_branch_id: int,
    to_branch_id: int,
    allowed_branch_ids: set[int] | None,
) -> tuple[Branch, Branch]:
    if allowed_branch_ids is not None and (
        from_branch_id not in allowed_branch_ids or to_branch_id not in allowed_branch_ids
    ):
        raise NotFoundException(code="not_found")
    if from_branch_id == to_branch_id:
        raise ValidationException(
            _("Choose a different target branch."),
            code="same_branch",
            fields={"to_branch": [_("Choose a different branch.")]},
        )
    branches = Branch.objects.select_for_update().filter(pk__in=sorted({from_branch_id, to_branch_id}))
    branch_by_id = {branch.pk: branch for branch in branches}
    from_branch = branch_by_id.get(from_branch_id)
    to_branch = branch_by_id.get(to_branch_id)
    if from_branch is None:
        raise ValidationException(_("Current branch is unavailable."), code="invalid_source_branch")
    if to_branch is None or not to_branch.is_active or to_branch.archived_at is not None:
        raise ValidationException(
            _("Choose an active target branch."),
            code="invalid_target_branch",
            fields={"to_branch": [_("Choose an active branch.")]},
        )
    return from_branch, to_branch


def _target_department(*, department_id: int | None, to_branch: Branch) -> Department | None:
    if department_id is None:
        return None
    department = (
        Department.objects.select_for_update()
        .filter(
            pk=department_id,
            branch=to_branch,
            is_active=True,
        )
        .first()
    )
    if department is None:
        raise ValidationException(
            _("Choose an active department in the target branch."),
            code="department_branch_mismatch",
            fields={"to_department": [_("Choose a department in the target branch.")]},
        )
    return department


def _move_principal_memberships(
    *,
    user_id: int,
    from_branch: Branch,
    to_branch: Branch,
    to_department: Department | None,
    account_kind: str,
    legacy_roles: tuple[str, ...],
) -> int:
    """Move active grants without broadening a department scope accidentally."""
    from apps.users.models import RoleMembership

    rows = list(
        RoleMembership.objects.select_for_update(of=("self",))
        .select_related("account_type")
        .filter(user_id=user_id)
        .order_by("pk")
    )
    matching = [
        row
        for row in rows
        if row.revoked_at is None
        and row.branch_id == from_branch.pk
        and (
            (
                row.account_type_id is not None
                and row.account_type is not None
                and row.account_type.account_kind == account_kind
            )
            or (row.account_type_id is None and row.role in legacy_roles)
        )
    ]
    now = timezone.now()
    for row in matching:
        duplicate = next(
            (
                candidate
                for candidate in rows
                if candidate.pk != row.pk
                and candidate.branch_id == to_branch.pk
                and candidate.department_id == (to_department.pk if to_department else None)
                and candidate.account_type_id == row.account_type_id
                and (candidate.account_type_id is not None or candidate.role == row.role)
            ),
            None,
        )
        if duplicate is not None:
            if duplicate.revoked_at is not None:
                duplicate.revoked_at = None
                duplicate.save(update_fields=["revoked_at"])
            row.revoked_at = now
            row.save(update_fields=["revoked_at"])
            continue
        row.branch = to_branch
        row.department = to_department
        row.save(update_fields=["branch", "department"])
    return len(matching)


@transaction.atomic
def transfer_teacher(
    *,
    teacher_id: int,
    to_branch_id: int,
    to_department_id: int | None = None,
    reason: str = "",
    confirm_impacts: bool = False,
    actor=None,
    actor_principal_kind: str = "",
    actor_principal_id: int | None = None,
    allowed_branch_ids: set[int] | None = None,
) -> BranchTransfer:
    """Move a teacher and end source-branch work that cannot follow them safely."""
    from apps.access.models import AccountType
    from apps.cohorts.models import Cohort, CohortTeacher
    from apps.schedule.models import Lesson, RecurrenceRule
    from apps.teachers.models import TeacherProfile
    from apps.users.models import User

    teacher = (
        TeacherProfile.objects.select_for_update()
        .select_related("branch", "user")
        .filter(pk=teacher_id)
        .first()
    )
    if teacher is None:
        raise NotFoundException(_("Teacher not found."), code="not_found")
    from_branch, to_branch = _locked_transfer_branches(
        from_branch_id=teacher.branch_id,
        to_branch_id=to_branch_id,
        allowed_branch_ids=allowed_branch_ids,
    )
    to_department = _target_department(department_id=to_department_id, to_branch=to_branch)
    User.objects.select_for_update().get(pk=teacher.user_id)

    assignment_count = CohortTeacher.objects.filter(
        teacher=teacher,
        cohort__branch=from_branch,
    ).count()
    primary_count = Cohort.objects.filter(
        branch=from_branch,
        primary_teacher=teacher,
    ).count()
    rule_count = RecurrenceRule.objects.filter(
        teacher=teacher,
        cohort__branch=from_branch,
        is_active=True,
    ).count()
    future_lessons = Lesson.objects.filter(
        teacher=teacher,
        cohort__branch=from_branch,
        starts_at__gte=timezone.now(),
        status=Lesson.Status.SCHEDULED,
    ).count()
    impact_total = assignment_count + primary_count + rule_count + future_lessons
    if impact_total and not confirm_impacts:
        raise ValidationException(
            _("Confirm the teaching and schedule changes before moving this teacher."),
            code="transfer_confirmation_required",
            fields={"confirm_impacts": [_("Review and confirm the transfer impact.")]},
        )

    CohortTeacher.objects.filter(teacher=teacher, cohort__branch=from_branch).delete()
    Cohort.objects.filter(branch=from_branch, primary_teacher=teacher).update(primary_teacher=None)
    RecurrenceRule.objects.filter(
        teacher=teacher,
        cohort__branch=from_branch,
        is_active=True,
    ).update(is_active=False, updated_at=timezone.now())
    Lesson.objects.filter(
        teacher=teacher,
        cohort__branch=from_branch,
        starts_at__gte=timezone.now(),
        status=Lesson.Status.SCHEDULED,
    ).update(
        status=Lesson.Status.CANCELLED,
        cancel_reason=(f"Teacher branch transfer: {reason.strip()}"[:255]),
        updated_at=timezone.now(),
    )

    ensure_role_membership(
        teacher,
        branch=to_branch,
        department=to_department,
        role=Role.TEACHER,
    )
    _move_principal_memberships(
        user_id=teacher.user_id,
        from_branch=from_branch,
        to_branch=to_branch,
        to_department=to_department,
        account_kind=AccountType.AccountKind.TEACHER,
        legacy_roles=(Role.TEACHER,),
    )
    teacher.branch = to_branch
    teacher.department = to_department
    teacher.save(update_fields=["branch", "department", "updated_at"])
    return record_transfer(
        user=teacher.user,
        subject_kind=BranchTransfer.SubjectKind.TEACHER,
        subject_id=teacher.pk,
        subject_name=teacher.get_full_name() or teacher.username or f"Teacher {teacher.pk}",
        subject_reference=teacher.username or "",
        from_branch=from_branch,
        to_branch=to_branch,
        reason=reason,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )


@transaction.atomic
def transfer_staff(
    *,
    staff_id: int,
    from_branch_id: int,
    to_branch_id: int,
    to_department_id: int | None = None,
    reason: str = "",
    actor=None,
    actor_principal_kind: str = "",
    actor_principal_id: int | None = None,
    allowed_branch_ids: set[int] | None = None,
) -> BranchTransfer:
    """Move every active staff responsibility from one branch to another."""
    from apps.access.models import AccountType
    from apps.users.models import User

    staff = StaffProfile.objects.select_for_update().select_related("user").filter(pk=staff_id).first()
    if staff is None:
        raise NotFoundException(_("Staff member not found."), code="not_found")
    from_branch, to_branch = _locked_transfer_branches(
        from_branch_id=from_branch_id,
        to_branch_id=to_branch_id,
        allowed_branch_ids=allowed_branch_ids,
    )
    to_department = _target_department(department_id=to_department_id, to_branch=to_branch)
    User.objects.select_for_update().get(pk=staff.user_id)
    moved = _move_principal_memberships(
        user_id=staff.user_id,
        from_branch=from_branch,
        to_branch=to_branch,
        to_department=to_department,
        account_kind=AccountType.AccountKind.STAFF,
        legacy_roles=STAFF_ROLES,
    )
    if moved == 0:
        raise ValidationException(
            _("This staff member has no active responsibilities in the selected branch."),
            code="invalid_source_branch",
            fields={"from_branch": [_("Choose a branch where this person currently works.")]},
        )
    return record_transfer(
        user=staff.user,
        subject_kind=BranchTransfer.SubjectKind.STAFF,
        subject_id=staff.pk,
        subject_name=staff.get_full_name() or staff.username or f"Staff {staff.pk}",
        subject_reference=staff.username or "",
        from_branch=from_branch,
        to_branch=to_branch,
        reason=reason,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )


@transaction.atomic
def transfer_cohort(
    *,
    cohort_id: int,
    to_branch_id: int,
    to_department_id: int | None = None,
    reason: str = "",
    confirm_impacts: bool = False,
    actor=None,
    actor_principal_kind: str = "",
    actor_principal_id: int | None = None,
    allowed_branch_ids: set[int] | None = None,
) -> BranchTransfer:
    """Move a whole group and its active students as one database transaction."""
    from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher
    from apps.schedule.models import Lesson, RecurrenceRule

    cohort = Cohort.objects.select_for_update().select_related("branch").filter(pk=cohort_id).first()
    if cohort is None:
        raise NotFoundException(_("Group not found."), code="not_found")
    if cohort.is_archived:
        raise ValidationException(_("An archived group cannot be moved."), code="archived_group")
    from_branch, to_branch = _locked_transfer_branches(
        from_branch_id=cohort.branch_id,
        to_branch_id=to_branch_id,
        allowed_branch_ids=allowed_branch_ids,
    )
    if Cohort.objects.filter(branch=to_branch, name__iexact=cohort.name).exclude(pk=cohort.pk).exists():
        raise ValidationException(
            _("A group with this name already exists in the target branch."),
            code="duplicate_group",
            fields={"to_branch": [_("Choose another branch or rename the group first.")]},
        )
    to_department = _target_department(department_id=to_department_id, to_branch=to_branch)
    memberships = list(
        CohortMembership.objects.select_for_update()
        .select_related("student", "student__user")
        .filter(cohort=cohort, end_date__isnull=True)
        .order_by("pk")
    )
    teacher_count = CohortTeacher.objects.filter(cohort=cohort).count()
    rule_count = RecurrenceRule.objects.filter(cohort=cohort, is_active=True).count()
    future_lessons = Lesson.objects.filter(
        cohort=cohort,
        starts_at__gte=timezone.now(),
        status=Lesson.Status.SCHEDULED,
    ).count()
    if (
        memberships or teacher_count or cohort.primary_teacher_id or rule_count or future_lessons
    ) and not confirm_impacts:
        raise ValidationException(
            _("Confirm the student, teaching, and schedule changes before moving this group."),
            code="transfer_confirmation_required",
            fields={"confirm_impacts": [_("Review and confirm the transfer impact.")]},
        )

    CohortTeacher.objects.filter(cohort=cohort).delete()
    RecurrenceRule.objects.filter(cohort=cohort, is_active=True).update(
        is_active=False,
        updated_at=timezone.now(),
    )
    Lesson.objects.filter(
        cohort=cohort,
        starts_at__gte=timezone.now(),
        status=Lesson.Status.SCHEDULED,
    ).update(
        status=Lesson.Status.CANCELLED,
        cancel_reason=(f"Group branch transfer: {reason.strip()}"[:255]),
        updated_at=timezone.now(),
    )
    cohort.branch = to_branch
    cohort.department = to_department
    cohort.primary_teacher = None
    cohort.default_room = None
    cohort.full_clean()
    cohort.save(update_fields=["branch", "department", "primary_teacher", "default_room", "updated_at"])

    for membership in memberships:
        transfer_student(
            student_id=membership.student_id,
            to_branch_id=to_branch.pk,
            reason=reason,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
            allowed_branch_ids=allowed_branch_ids,
        )
    return record_transfer(
        user=None,
        subject_kind=BranchTransfer.SubjectKind.COHORT,
        subject_id=cohort.pk,
        subject_name=cohort.name,
        subject_reference=f"group-{cohort.pk}",
        from_branch=from_branch,
        to_branch=to_branch,
        reason=reason,
        actor=actor,
        actor_principal_kind=actor_principal_kind,
        actor_principal_id=actor_principal_id,
    )


def _align_student_account_type_scopes(
    *,
    student,
    to_branch: Branch,
    account_type_model,
    membership_model,
) -> None:
    """Move branch-wide student grants; revoke unmappable department grants."""
    memberships = list(
        membership_model.objects.select_for_update(of=("self",))
        .select_related("account_type")
        .filter(user_id=student.user_id)
    )
    student_rows = [
        membership
        for membership in memberships
        if (
            membership.account_type_id is not None
            and membership.account_type.account_kind == account_type_model.AccountKind.STUDENT
        )
        or (membership.account_type_id is None and membership.role == Role.STUDENT)
    ]
    student_grants = [membership for membership in student_rows if membership.revoked_at is None]
    now = timezone.now()
    for membership in student_grants:
        if membership.branch_id == to_branch.pk and membership.department_id is None:
            continue
        # A department identifier cannot be mapped across branches safely. The
        # canonical system membership was already normalized by ensure_role_membership;
        # any remaining department-specific custom type is revoked, never broadened.
        if membership.department_id is not None:
            membership.revoked_at = now
            membership.save(update_fields=["revoked_at"])
            continue
        duplicate = next(
            (
                candidate
                for candidate in student_rows
                if candidate.pk != membership.pk
                and candidate.branch_id == to_branch.pk
                and candidate.department_id is None
                and candidate.account_type_id == membership.account_type_id
                and (candidate.account_type_id is not None or candidate.role == membership.role)
            ),
            None,
        )
        if duplicate is not None:
            if duplicate.revoked_at is not None:
                duplicate.revoked_at = None
                duplicate.save(update_fields=["revoked_at"])
            membership.revoked_at = now
            membership.save(update_fields=["revoked_at"])
            continue
        membership.branch = to_branch
        membership.department = None
        membership.save(update_fields=["branch", "department"])
