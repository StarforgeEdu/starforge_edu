"""Shared validation for HTTP and programmatic permission-override writes."""

from django.utils.translation import gettext_lazy as _

from apps.access.models import RolePermissionOverride
from core.exceptions import ValidationException
from core.permissions import ROLE_PERMISSION_MATRIX, Role

# Permission gates that are currently held only through the director's ``*:*``.
# They remain valid A-2 override targets even though no named role has them in the
# default matrix. Keep this list aligned with new check_perm/has_permission_code
# gates that are intentionally delegable; ``access:*`` is deliberately excluded.
_IMPLICIT_DIRECTOR_PERMISSION_CODES = {
    "ai:manage",
    "assignments:write",
    "cohorts:write",
    "content:write",
    "finance:write",
    "notifications:write",
    "org:write",
    "parents:write",
    "payments:read",
    "schedule:write",
    "students:write",
    "teachers:write",
}


def permission_catalogue() -> set[str]:
    """Concrete delegable codes advertised by the access API."""
    codes = set(_IMPLICIT_DIRECTOR_PERMISSION_CODES)
    for permissions in ROLE_PERMISSION_MATRIX.values():
        codes.update(permissions)
    return {code for code in codes if code != "*:*" and not code.startswith("access:")}


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


def validate_effect(value: str) -> str:
    if value not in RolePermissionOverride.Effect.values:
        raise ValidationException(
            _("Invalid effect."),
            code="validation_error",
            fields={"effect": [f"Must be one of {', '.join(RolePermissionOverride.Effect.values)}."]},
        )
    return value
