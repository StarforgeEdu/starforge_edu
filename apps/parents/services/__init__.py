"""Parent / guardian write services (TASKS §6).

Kept as module-level domain functions (not only the layered service classes)
because tests import them directly:
``from apps.parents.services import create_parent, link_guardian``.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from apps.parents.models import Guardian, ParentProfile
from apps.users.services import create_role_user_bridge, ensure_role_membership, prepare_role_identity
from core.exceptions import ValidationException
from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES, ScopeAttributionStatus
from core.permissions import Role


@transaction.atomic
def create_parent(
    *,
    phone: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    middle_name: str = "",
    birthdate=None,
    gender: str = "",
    workplace: str = "",
    notes: str = "",
    username: str = "",
    branch_at_creation=None,
    department_at_creation=None,
    attribution_status: str = ScopeAttributionStatus.UNRESOLVED,
    created_by=None,
) -> ParentProfile:
    identity = prepare_role_identity(
        phone=phone, email=email, first_name=first_name, last_name=last_name, middle_name=middle_name
    )
    if not identity["phone"] and not identity["email"]:
        raise ValidationException(_("phone or email is required."), code="identifier_required")
    if (identity["phone"] and ParentProfile.objects.filter(phone=identity["phone"]).exists()) or (
        identity["email"] and ParentProfile.objects.filter(email__iexact=identity["email"]).exists()
    ):
        raise ValidationException(_("This person already has a parent profile."), code="duplicate_parent")
    if attribution_status not in ScopeAttributionStatus.values:
        raise ValidationException(
            _("Invalid creation-scope attribution status."),
            code="validation_error",
            fields={"attribution_status": ["Not a valid choice."]},
        )
    if department_at_creation is not None and (
        branch_at_creation is None or department_at_creation.branch_id != branch_at_creation.pk
    ):
        raise ValidationException(
            _("The creation department must belong to the creation branch."),
            code="validation_error",
            fields={"department": ["Not found or not in the selected branch."]},
        )
    if attribution_status in ATTRIBUTED_SCOPE_STATUSES:
        if branch_at_creation is None:
            raise ValidationException(
                _("An attributed parent requires a creation branch."),
                code="validation_error",
                fields={"branch": ["This field is required."]},
            )
    elif branch_at_creation is not None or department_at_creation is not None:
        raise ValidationException(
            _("Unresolved parent attribution cannot carry a branch or department."),
            code="validation_error",
            fields={"branch": ["Resolve the attribution status before assigning scope."]},
        )
    user, username, identity = create_role_user_bridge(username=username, **identity)
    return ParentProfile.objects.create(
        user=user,
        # Identity and credentials are owned by the parent account. The linked User is
        # an internal, password-disabled authorization bridge and is never operator-facing.
        username=username,
        password=user.password,
        first_name=identity["first_name"],
        last_name=identity["last_name"],
        middle_name=identity["middle_name"],
        phone=identity["phone"],
        email=identity["email"],
        birthdate=birthdate,
        gender=gender,
        workplace=workplace,
        notes=notes,
        branch_at_creation=branch_at_creation,
        department_at_creation=department_at_creation,
        attribution_status=attribution_status,
        created_by=created_by,
    )


@transaction.atomic
def link_guardian(
    *,
    parent: ParentProfile,
    student,
    relationship: str,
    is_primary: bool = False,
    custody_notes: str = "",
    actor=None,
) -> Guardian:
    """Link a parent to a student. Enforces one primary guardian per student
    (also a DB constraint) and prevents duplicate links, returning clean 400s."""
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_log
    from apps.students.models import StudentProfile
    from apps.users.models import User
    from core.identity_lifecycle import assert_exclusive_role_bridge

    # These rows are the complete identity and authorization boundary touched by
    # the link. Stable locking also serializes direct service callers with parent
    # deactivation, student transfer/deactivation, and concurrent link attempts.
    parent = ParentProfile.objects.select_for_update(of=("self",)).select_related("user").get(pk=parent.pk)
    student = (
        StudentProfile.objects.select_for_update(of=("self",))
        .select_related("user", "current_cohort")
        .get(pk=student.pk)
    )
    list(
        User.objects.select_for_update()
        .filter(pk__in=sorted({parent.user_id, student.user_id}))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    assert_exclusive_role_bridge(parent, principal_kind="parent")
    assert_exclusive_role_bridge(student, principal_kind="student")
    if relationship not in Guardian.Relationship.values:
        raise ValidationException(
            _("Invalid relationship."),
            code="validation_error",
            fields={"relationship": [_("Choose a supported relationship.")]},
        )
    if not isinstance(is_primary, bool):
        raise ValidationException(
            _("Invalid primary-guardian flag."),
            code="validation_error",
            fields={"is_primary": [_("Must be a boolean.")]},
        )
    if not parent.is_active or not parent.user.is_active:
        raise ValidationException(
            _("The selected parent account is inactive."),
            code="invalid_parent",
            fields={"parent": [_("Choose an active parent.")]},
        )
    if not student.is_active or not student.user.is_active:
        raise ValidationException(
            _("The selected student account is inactive."),
            code="invalid_student",
            fields={"student": [_("Choose an active student.")]},
        )
    if Guardian.objects.filter(parent=parent, student=student, revoked_at__isnull=True).exists():
        raise ValidationException(_("This guardian link already exists."), code="guardian_exists")
    if (
        is_primary
        and Guardian.objects.filter(
            student=student,
            is_primary=True,
            revoked_at__isnull=True,
        ).exists()
    ):
        raise ValidationException(
            _("This student already has a primary guardian."), code="primary_guardian_exists"
        )
    try:
        # A savepoint lets us translate the DB uniqueness winner cleanly when
        # concurrent requests passed the advisory existence checks.
        with transaction.atomic():
            guardian = Guardian.objects.create(
                parent=parent,
                student=student,
                relationship=relationship,
                is_primary=is_primary,
                custody_notes=custody_notes,
            )
    except IntegrityError:
        if Guardian.objects.filter(parent=parent, student=student, revoked_at__isnull=True).exists():
            raise ValidationException(
                _("This guardian link already exists."),
                code="guardian_exists",
            ) from None
        if (
            is_primary
            and Guardian.objects.filter(
                student=student,
                is_primary=True,
                revoked_at__isnull=True,
            ).exists()
        ):
            raise ValidationException(
                _("This student already has a primary guardian."),
                code="primary_guardian_exists",
            ) from None
        raise
    ensure_role_membership(
        parent,
        branch=student.branch,
        department=None,
        role=Role.PARENT,
        replace_scope=False,
    )
    cohort = student.current_cohort
    audit_log(
        actor=actor,
        action="create",
        resource_type="parents.Guardian",
        resource_id=guardian.pk,
        after={
            "parent_id": parent.pk,
            "student_id": student.pk,
            "relationship": guardian.relationship,
            "is_primary": guardian.is_primary,
            "custody_notes": custody_notes,
        },
        scope=scoped_audit_scope(
            student.branch_id,
            cohort.department_id if cohort is not None else None,
        ),
    )
    return guardian
