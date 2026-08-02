"""Role-native recipient attribution and notification-feed isolation.

The internal ``users.User`` row is a compatibility bridge and may be shared by
student, teacher, parent, and staff accounts.  It is therefore never sufficient
as the recipient of private notification state.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from django.apps import apps as django_apps

from apps.notifications.models import (
    DELIVERABLE_ATTRIBUTION_STATUSES,
    RecipientAttributionStatus,
    RecipientPrincipalKind,
)

_PRINCIPAL_MODELS: dict[str, str] = {
    RecipientPrincipalKind.STUDENT: "students.StudentProfile",
    RecipientPrincipalKind.TEACHER: "teachers.TeacherProfile",
    RecipientPrincipalKind.PARENT: "parents.ParentProfile",
    RecipientPrincipalKind.STAFF: "org.StaffProfile",
}

_LEGACY_ROLE_KINDS = {
    "student": RecipientPrincipalKind.STUDENT,
    "teacher": RecipientPrincipalKind.TEACHER,
    "parent": RecipientPrincipalKind.PARENT,
}


@dataclass(frozen=True, slots=True)
class RecipientPrincipal:
    user_id: int
    kind: str | None
    principal_id: int | None
    status: str
    reason: str

    @property
    def is_deliverable(self) -> bool:
        return self.status in DELIVERABLE_ATTRIBUTION_STATUSES


@dataclass(frozen=True, slots=True)
class ResolvedRecipientPrincipal:
    """A recipient whose role-native identity is statically non-null."""

    user_id: int
    kind: str
    principal_id: int
    status: str
    reason: str

    @property
    def is_deliverable(self) -> bool:
        return True


def _positive_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _profile_evidence_for_users(user_ids: set[int]) -> dict[int, dict[str, tuple[int, bool]]]:
    evidence: dict[int, dict[str, tuple[int, bool]]] = defaultdict(dict)
    for kind, label in _PRINCIPAL_MODELS.items():
        model = django_apps.get_model(label)
        for user_id, profile_id, is_active in model.objects.filter(user_id__in=user_ids).values_list(
            "user_id", "pk", "is_active"
        ):
            evidence[int(user_id)][str(kind)] = (int(profile_id), bool(is_active))
    return evidence


def _membership_kind_evidence_for_users(user_ids: set[int]) -> dict[int, set[str]]:
    """Return every current or historical account kind assigned to the bridge.

    Revoked memberships and inactive account types still matter when reviewing
    old rows: ignoring them could misclassify a notification created while that
    other role was active.
    """

    RoleMembership = django_apps.get_model("users.RoleMembership")
    kinds: dict[int, set[str]] = defaultdict(set)
    rows = RoleMembership.objects.filter(user_id__in=user_ids).values_list(
        "user_id", "account_type__account_kind", "role"
    )
    valid = set(RecipientPrincipalKind.values)
    for user_id, account_kind, legacy_role in rows:
        if account_kind in valid:
            kinds[int(user_id)].add(str(account_kind))
        elif legacy_role:
            kinds[int(user_id)].add(
                str(_LEGACY_ROLE_KINDS.get(str(legacy_role), RecipientPrincipalKind.STAFF))
            )
    return kinds


def _implicit_resolution(
    *,
    user_id: int,
    is_live_user: bool,
    profiles: dict[str, tuple[int, bool]],
    membership_kinds: set[str],
) -> RecipientPrincipal:
    if not is_live_user:
        return RecipientPrincipal(
            user_id=user_id,
            kind=None,
            principal_id=None,
            status=RecipientAttributionStatus.QUARANTINED,
            reason="inactive_or_missing_user",
        )
    evidence_kinds = set(profiles) | membership_kinds
    if len(evidence_kinds) > 1:
        return RecipientPrincipal(
            user_id=user_id,
            kind=None,
            principal_id=None,
            status=RecipientAttributionStatus.CONFLICTING,
            reason="multiple_role_kinds",
        )
    if len(profiles) != 1 or len(evidence_kinds) != 1:
        return RecipientPrincipal(
            user_id=user_id,
            kind=None,
            principal_id=None,
            status=RecipientAttributionStatus.UNRESOLVED,
            reason="missing_role_profile",
        )
    kind, (profile_id, is_active) = next(iter(profiles.items()))
    if kind not in evidence_kinds or not is_active:
        return RecipientPrincipal(
            user_id=user_id,
            kind=None,
            principal_id=None,
            status=RecipientAttributionStatus.UNRESOLVED,
            reason="inactive_or_inconsistent_profile",
        )
    return RecipientPrincipal(
        user_id=user_id,
        kind=kind,
        principal_id=profile_id,
        status=RecipientAttributionStatus.CAPTURED,
        reason="single_role_evidence",
    )


def resolve_recipient_principals(user_ids: Iterable[int]) -> dict[int, RecipientPrincipal]:
    """Resolve omitted attribution for a batch in a constant query count."""

    normalized = {
        user_id
        for user_id in user_ids
        if isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0
    }
    if not normalized:
        return {}
    User = django_apps.get_model("users.User")
    live_users = set(User.objects.filter(pk__in=normalized, is_active=True).values_list("pk", flat=True))
    profiles_by_user = _profile_evidence_for_users(normalized)
    memberships_by_user = _membership_kind_evidence_for_users(normalized)
    return {
        user_id: _implicit_resolution(
            user_id=user_id,
            is_live_user=user_id in live_users,
            profiles=profiles_by_user.get(user_id, {}),
            membership_kinds=memberships_by_user.get(user_id, set()),
        )
        for user_id in normalized
    }


def resolve_recipient_principal(
    *,
    user_id: int,
    principal_kind: str | None = None,
    principal_id: object = None,
    user_is_active: bool | None = None,
) -> RecipientPrincipal:
    """Validate an explicit role recipient or conservatively infer one.

    Explicit attribution is accepted only when the active role-native profile
    exists and belongs to ``user_id``.  Omitted attribution is inferred only
    when all durable profile and membership evidence names one account kind and
    exactly one active role profile supplies its real primary key.  Everything
    else is retained as non-deliverable quarantine state.
    """

    if _positive_id(user_id) is None:
        return RecipientPrincipal(
            user_id=user_id,
            kind=None,
            principal_id=None,
            status=RecipientAttributionStatus.QUARANTINED,
            reason="invalid_user_id",
        )

    normalized_kind = str(principal_kind or "")
    normalized_id = _positive_id(principal_id)
    if normalized_kind or principal_id is not None:
        if user_is_active is None:
            User = django_apps.get_model("users.User")
            user_is_active = User.objects.filter(pk=user_id, is_active=True).exists()
        if not user_is_active:
            return RecipientPrincipal(
                user_id=user_id,
                kind=None,
                principal_id=None,
                status=RecipientAttributionStatus.QUARANTINED,
                reason="inactive_or_missing_user",
            )
        label = _PRINCIPAL_MODELS.get(normalized_kind)
        if label is None or normalized_id is None:
            return RecipientPrincipal(
                user_id=user_id,
                kind=None,
                principal_id=None,
                status=RecipientAttributionStatus.QUARANTINED,
                reason="invalid_explicit_principal",
            )
        model = django_apps.get_model(label)
        if not model.objects.filter(
            pk=normalized_id,
            user_id=user_id,
            is_active=True,
        ).exists():
            return RecipientPrincipal(
                user_id=user_id,
                kind=None,
                principal_id=None,
                status=RecipientAttributionStatus.QUARANTINED,
                reason="principal_not_active_or_not_owned",
            )
        return RecipientPrincipal(
            user_id=user_id,
            kind=normalized_kind,
            principal_id=normalized_id,
            status=RecipientAttributionStatus.CAPTURED,
            reason="explicit_role_principal",
        )

    return resolve_recipient_principals((user_id,))[user_id]


def request_notification_principal(request) -> ResolvedRecipientPrincipal:
    """Return the exact current recipient or deny a non-role/ambiguous session."""

    resolution = resolve_recipient_principal(
        user_id=request.user.pk,
        principal_kind=str(getattr(request, "principal_kind", "") or ""),
        principal_id=getattr(request, "principal_id", None),
    )
    if resolution.is_deliverable and resolution.kind is not None and resolution.principal_id is not None:
        return ResolvedRecipientPrincipal(
            user_id=resolution.user_id,
            kind=resolution.kind,
            principal_id=resolution.principal_id,
            status=resolution.status,
            reason=resolution.reason,
        )
    from core.exceptions import PermissionException

    raise PermissionException(
        "Notifications are unavailable for this account session.",
        code="principal_feed_unavailable",
    )


def principal_feed_is_unambiguous(*, user_id: int, principal_kind: str, principal_id: object) -> bool:
    """Compatibility predicate used by the async WebSocket authorization path."""

    return resolve_recipient_principal(
        user_id=user_id,
        principal_kind=principal_kind,
        principal_id=principal_id,
    ).is_deliverable


def enforce_principal_feed_isolation(request) -> None:
    """Compatibility wrapper retained for callers during the additive rollout."""

    request_notification_principal(request)
