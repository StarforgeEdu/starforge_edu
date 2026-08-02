"""Fail-closed helpers for destructive role-account lifecycle operations.

The role-native account tables own credentials while ``users.User`` remains a
compatibility bridge for permissions, sessions, devices, and historical foreign
keys.  Legacy data can still contain more than one role profile on one bridge.
Mutating or deactivating such a bridge as though it represented one role would
revoke (or rename) every other principal attached to it, so lifecycle writes must
refuse ambiguous bridges and send them to a reviewed repair workflow.
"""

from __future__ import annotations

from django.apps import apps as django_apps
from django.utils.translation import gettext_lazy as _

from core.exceptions import ConflictException
from core.role_principals import PRINCIPAL_MODELS


def assert_exclusive_role_bridge(account, *, principal_kind: str) -> None:
    """Require ``account.user`` to back this role profile and no other profile.

    New role accounts receive a dedicated bridge, but this check protects legacy
    multi-role users.  Callers lock the bridge user before invoking it so two
    lifecycle operations on the same compatibility principal serialize.
    """

    if principal_kind not in PRINCIPAL_MODELS:
        raise ValueError("principal_kind must be a known role principal")
    if not account.pk or not account.user_id:
        raise ConflictException(
            _("This account identity requires review before it can be changed."),
            code="identity_bridge_unresolved",
        )

    matches: list[tuple[str, int]] = []
    for kind, model_label in PRINCIPAL_MODELS.items():
        model = django_apps.get_model(model_label)
        principal_ids = model.objects.filter(user_id=account.user_id).values_list("pk", flat=True)[:2]
        matches.extend((kind, int(principal_id)) for principal_id in principal_ids)
        if len(matches) > 1:
            break

    if matches != [(principal_kind, int(account.pk))]:
        raise ConflictException(
            _("This account identity requires review before it can be changed."),
            code="identity_bridge_ambiguous",
        )
