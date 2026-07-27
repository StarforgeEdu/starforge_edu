"""Parent / guardian write services (TASKS §6).

Kept as module-level domain functions (not only the layered service classes)
because tests import them directly:
``from apps.parents.services import create_parent, link_guardian``.
"""

from __future__ import annotations

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.parents.models import Guardian, ParentProfile
from apps.users.models import RoleMembership
from apps.users.services import create_role_user_bridge, prepare_role_identity
from core.exceptions import ValidationException
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
    )


@transaction.atomic
def link_guardian(
    *, parent: ParentProfile, student, relationship: str, is_primary: bool = False, custody_notes: str = ""
) -> Guardian:
    """Link a parent to a student. Enforces one primary guardian per student
    (also a DB constraint) and prevents duplicate links, returning clean 400s."""
    if Guardian.objects.filter(parent=parent, student=student).exists():
        raise ValidationException(_("This guardian link already exists."), code="guardian_exists")
    if is_primary and Guardian.objects.filter(student=student, is_primary=True).exists():
        raise ValidationException(
            _("This student already has a primary guardian."), code="primary_guardian_exists"
        )
    guardian = Guardian.objects.create(
        parent=parent,
        student=student,
        relationship=relationship,
        is_primary=is_primary,
        custody_notes=custody_notes,
    )
    RoleMembership.objects.get_or_create(
        user=parent.user,
        branch=student.branch,
        department=None,
        role=Role.PARENT,
    )
    return guardian
