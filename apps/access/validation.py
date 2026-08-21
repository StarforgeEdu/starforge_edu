"""Shared validation for account types and compatibility override writes."""

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_slug
from django.utils.translation import gettext_lazy as _

from apps.access.models import RolePermissionOverride
from core.exceptions import ValidationException
from core.permissions import ROLE_PERMISSION_MATRIX, Role

if TYPE_CHECKING:
    from apps.access.models import AccountType

# Permission gates that are currently held only through the director's ``*:*``.
# They remain valid A-2 override targets even though no named role has them in the
# default matrix. Keep this list aligned with new check_perm/has_permission_code
# gates that are intentionally delegable; ``access:*`` is deliberately excluded.
_IMPLICIT_DIRECTOR_PERMISSION_CODES = {
    "ai:manage",
    "assignments:write",
    "cohorts:write",
    "compensation:approve",
    "compensation:disburse",
    "compensation:read",
    "compensation:run",
    "compensation:write",
    "content:write",
    "crm:manage",
    "finance:write",
    "notifications:write",
    "org:write",
    "organization_settings:read",
    "organization_settings:write",
    "parents:write",
    "payments:read",
    "schedule:write",
    "students:write",
    "tasks:transition_any",
    "teachers:write",
    "system:read",
    "system:write",
}


def permission_catalogue() -> set[str]:
    """Concrete delegable codes advertised by the access API."""
    codes = set(_IMPLICIT_DIRECTOR_PERMISSION_CODES)
    for permissions in ROLE_PERMISSION_MATRIX.values():
        codes.update(permissions)
    return {code for code in codes if code != "*:*" and not code.startswith("access:")}


def permission_catalogue_metadata() -> list[dict[str, str]]:
    """Human-readable metadata for access-management clients."""
    descriptions = {
        "tasks:assign_any": _("Assign tasks without the normal role-grade restriction."),
        "tasks:transition_any": _("Transition a task currently assigned to another principal."),
    }
    items: list[dict[str, str]] = []
    for code in sorted(permission_catalogue()):
        resource, verb = code.split(":", 1)
        label = _("%(resource)s: %(verb)s") % {
            "resource": resource.replace("_", " ").title(),
            "verb": _("All actions") if verb == "*" else verb.replace("_", " ").title(),
        }
        description = descriptions.get(
            code,
            _("Allows %(verb)s operations on %(resource)s resources.")
            % {
                "verb": _("all") if verb == "*" else verb.replace("_", " "),
                "resource": resource.replace("_", " "),
            },
        )
        items.append({"code": code, "label": str(label), "description": str(description)})
    return items


def validate_role(value: str) -> str:
    if value not in Role.ALL:
        raise ValidationException(
            _("Unknown role."), code="validation_error", fields={"role": ["Unknown role."]}
        )
    return value


def validate_permission(value: str) -> str:
    value = (value or "").strip()
    if value == "*:*":
        raise ValidationException(
            _("The master wildcard '*:*' cannot be overridden."),
            code="validation_error",
            fields={"permission": ["The master wildcard '*:*' cannot be overridden."]},
        )
    resource, separator, verb = value.partition(":")
    if not separator or not resource or not verb:
        raise ValidationException(
            _("Permission must look like 'resource:verb' (e.g. students:write or students:*)."),
            code="validation_error",
            fields={"permission": ["Must look like 'resource:verb'."]},
        )
    if resource == "access":
        raise ValidationException(
            _("The 'access' resource cannot be overridden (permission management stays director-only)."),
            code="validation_error",
            fields={"permission": ["The 'access' resource cannot be overridden."]},
        )

    catalogue = permission_catalogue()
    known_resources = {code.partition(":")[0] for code in catalogue}
    if value not in catalogue and not (verb == "*" and resource in known_resources):
        raise ValidationException(
            _("Unknown permission code."),
            code="validation_error",
            fields={"permission": ["Choose a known permission code or resource:* wildcard."]},
        )
    return value


def validate_account_type_permission(value: str, *, account_type: "AccountType") -> str:
    """Validate one canonical grant for ``account_type``.

    Only the protected, migration-created owner type may hold the master or
    access-control wildcards. All other types are limited to the delegable
    permission catalogue used throughout the API.
    """
    value = (value or "").strip()
    resource, separator, verb = value.partition(":")
    if not separator or not resource or not verb:
        raise ValidationException(
            _("Permission must look like 'resource:verb' (e.g. students:write or students:*)."),
            code="validation_error",
            fields={"permission": [_("Must look like 'resource:verb'.")]},
        )
    if value == "*:*" or resource == "access":
        if not account_type.is_owner_type:
            raise ValidationException(
                _("Only the protected system owner type may hold master or access permissions."),
                code="protected_permission",
                fields={
                    "permission": [_("Master and access permissions are reserved for the system owner type.")]
                },
            )
        if value == "*:*" or verb in {"read", "write", "*"}:
            return value
        raise ValidationException(
            _("Unknown access permission code."),
            code="validation_error",
            fields={"permission": [_("Choose access:read, access:write, or access:*.")]},
        )
    return validate_permission(value)


def validate_account_kind(value: str) -> str:
    from apps.access.models import AccountType

    if value not in AccountType.AccountKind.values:
        raise ValidationException(
            _("Unknown account kind."),
            code="validation_error",
            fields={"account_kind": [_("Choose staff, teacher, student, or parent.")]},
        )
    return value


def validate_account_type_name(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValidationException(
            _("Account type name is required."),
            code="validation_error",
            fields={"name": [_("This field is required.")]},
        )
    return value


def validate_account_type_slug(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        raise ValidationException(
            _("Account type slug is required."),
            code="validation_error",
            fields={"slug": [_("This field is required.")]},
        )
    try:
        validate_slug(value)
    except DjangoValidationError as exc:
        raise ValidationException(
            _("Account type slug must contain only letters, numbers, underscores, or hyphens."),
            code="validation_error",
            fields={"slug": [_("Enter a valid slug.")]},
        ) from exc
    return value


def validate_effect(value: str) -> str:
    if value not in RolePermissionOverride.Effect.values:
        raise ValidationException(
            _("Invalid effect."),
            code="validation_error",
            fields={"effect": [f"Must be one of {', '.join(RolePermissionOverride.Effect.values)}."]},
        )
    return value
