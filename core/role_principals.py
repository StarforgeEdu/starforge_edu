"""Small, fail-closed helpers for role-native principal attribution.

``users.User`` is a compatibility bridge and can back more than one login role.  A
domain row that records only ``user_id`` therefore does not identify the principal
that may read or mutate it.  Workflow domains use these helpers when they capture an
immutable ``(kind, principal_id)`` alongside the bridge FK.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.apps import apps as django_apps

from core.exceptions import PermissionException, StrOrPromise, ValidationException

PRINCIPAL_MODELS: dict[str, str] = {
    "student": "students.StudentProfile",
    "teacher": "teachers.TeacherProfile",
    "parent": "parents.ParentProfile",
    "staff": "org.StaffProfile",
}
PRINCIPAL_KINDS = frozenset(PRINCIPAL_MODELS)
STAFF_PRINCIPAL_KINDS = frozenset({"staff", "teacher"})


@dataclass(frozen=True, slots=True)
class RolePrincipal:
    kind: str
    principal_id: int
    user_id: int


def _allowed_kinds(allowed_kinds: Iterable[str] | None) -> frozenset[str]:
    kinds = frozenset(allowed_kinds) if allowed_kinds is not None else PRINCIPAL_KINDS
    if not kinds or not kinds.issubset(PRINCIPAL_KINDS):
        raise ValueError("allowed_kinds must contain known role-principal kinds")
    return kinds


def _profile_exists(*, kind: str, principal_id: int, user_id: int) -> bool:
    model = django_apps.get_model(PRINCIPAL_MODELS[kind])
    return model.objects.filter(
        pk=principal_id,
        user_id=user_id,
        user__is_active=True,
        is_active=True,
    ).exists()


def request_role_principal(
    request: Any,
    *,
    allowed_kinds: Iterable[str] | None = None,
    error_code: str = "principal_unavailable",
) -> RolePrincipal:
    """Validate and return the exact role principal bound to this session."""

    kinds = _allowed_kinds(allowed_kinds)
    user = getattr(request, "user", None)
    kind = str(getattr(request, "principal_kind", "") or "")
    principal_id = getattr(request, "principal_id", None)
    user_id = getattr(user, "pk", None)
    from core.session_auth import session_validated_request_principal

    session_validated = session_validated_request_principal(request)
    if (
        kind not in kinds
        or isinstance(principal_id, bool)
        or not isinstance(principal_id, int)
        or principal_id <= 0
        or isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not bool(getattr(user, "is_active", False))
        or (
            not session_validated
            and not _profile_exists(kind=kind, principal_id=principal_id, user_id=user_id)
        )
    ):
        # A small compatibility surface remains for test fixtures that mint a
        # blank legacy session.  Authentication sets this marker server-side;
        # request input cannot opt into it and every non-test settings module
        # disables the backing flag.  Even here we resolve exactly one active
        # role account rather than restoring the old bridge-user union.
        if (
            not kind
            and principal_id is None
            and isinstance(user_id, int)
            and not isinstance(user_id, bool)
            and user_id > 0
            and getattr(request, "_allow_legacy_principal_union_for_tests", False)
        ):
            try:
                return resolve_unambiguous_user_principal(
                    user_id,
                    allowed_kinds=kinds,
                    field="principal",
                    message="The test session does not identify one active role account.",
                )
            except ValidationException:
                pass
        raise PermissionException(
            "This workflow is unavailable for the active account session.",
            code=error_code,
        )
    return RolePrincipal(kind=kind, principal_id=principal_id, user_id=user_id)


def validate_role_principal(
    *,
    kind: str,
    principal_id: int,
    user_id: int,
    allowed_kinds: Iterable[str] | None = None,
    field: str = "principal",
) -> RolePrincipal:
    """Validate an explicit stored/input principal against its active profile."""

    kinds = _allowed_kinds(allowed_kinds)
    if (
        kind not in kinds
        or isinstance(principal_id, bool)
        or not isinstance(principal_id, int)
        or principal_id <= 0
        or isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not _profile_exists(kind=kind, principal_id=principal_id, user_id=user_id)
    ):
        raise ValidationException(
            "The selected role account is not active.",
            code="validation_error",
            fields={field: ["Choose one active role account."]},
        )
    return RolePrincipal(kind=kind, principal_id=principal_id, user_id=user_id)


def resolve_unambiguous_user_principal(
    user_id: int,
    *,
    allowed_kinds: Iterable[str] | None = None,
    field: str,
    message: StrOrPromise,
) -> RolePrincipal:
    """Resolve a bridge user only when exactly one eligible active profile exists.

    Legacy DTOs select recipients by ``user_id``.  When that bridge backs both a
    teacher and staff profile there is no safe way to guess which account was meant;
    callers must receive one generic field error rather than silently granting both.
    """

    kinds = _allowed_kinds(allowed_kinds)
    matches: list[RolePrincipal] = []
    for kind in sorted(kinds):
        model = django_apps.get_model(PRINCIPAL_MODELS[kind])
        principal_id = (
            model.objects.filter(user_id=user_id, user__is_active=True, is_active=True)
            .values_list("pk", flat=True)
            .first()
        )
        if principal_id is not None:
            matches.append(RolePrincipal(kind=kind, principal_id=int(principal_id), user_id=user_id))
            if len(matches) > 1:
                break
    if len(matches) != 1:
        raise ValidationException(
            message,
            code="validation_error",
            fields={field: [message]},
        )
    return matches[0]


def resolve_unambiguous_user_principals(
    user_ids: Iterable[int],
    *,
    allowed_kinds: Iterable[str] | None = None,
    field: str,
    message: StrOrPromise,
) -> dict[int, RolePrincipal]:
    """Batch companion to :func:`resolve_unambiguous_user_principal`."""

    kinds = _allowed_kinds(allowed_kinds)
    requested = frozenset(user_ids)
    matches: dict[int, list[RolePrincipal]] = {user_id: [] for user_id in requested}
    for kind in sorted(kinds):
        model = django_apps.get_model(PRINCIPAL_MODELS[kind])
        for principal_id, user_id in model.objects.filter(
            user_id__in=requested,
            user__is_active=True,
            is_active=True,
        ).values_list("pk", "user_id"):
            matches[int(user_id)].append(
                RolePrincipal(kind=kind, principal_id=int(principal_id), user_id=int(user_id))
            )
    if any(len(rows) != 1 for rows in matches.values()):
        raise ValidationException(
            message,
            code="validation_error",
            fields={field: [message]},
        )
    return {user_id: rows[0] for user_id, rows in matches.items()}


def find_unambiguous_user_principals(
    user_ids: Iterable[int],
    *,
    allowed_kinds: Iterable[str] | None = None,
) -> dict[int, RolePrincipal]:
    """Return only safely attributable users from a batch, omitting ambiguous rows."""

    kinds = _allowed_kinds(allowed_kinds)
    requested = frozenset(user_ids)
    matches: dict[int, list[RolePrincipal]] = {user_id: [] for user_id in requested}
    for kind in sorted(kinds):
        model = django_apps.get_model(PRINCIPAL_MODELS[kind])
        for principal_id, user_id in model.objects.filter(
            user_id__in=requested,
            user__is_active=True,
            is_active=True,
        ).values_list("pk", "user_id"):
            matches[int(user_id)].append(
                RolePrincipal(kind=kind, principal_id=int(principal_id), user_id=int(user_id))
            )
    return {user_id: rows[0] for user_id, rows in matches.items() if len(rows) == 1}


def principal_filter_kwargs(principal: RolePrincipal, *, prefix: str = "") -> dict[str, object]:
    """Return ORM equality kwargs for a captured principal pair."""

    return {
        f"{prefix}principal_kind": principal.kind,
        f"{prefix}principal_id": principal.principal_id,
    }
