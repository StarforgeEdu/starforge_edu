"""Small, shared primitives for retry-safe role-principal mutations.

The raw retry key is capability-like input and must never be persisted.  Callers
store only :func:`principal_scoped_key_hash` and a semantic operation fingerprint
on the domain row created by the mutation.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from typing import Any

from django.db import connection
from django.utils.translation import gettext_lazy as _

from core.exceptions import PermissionException, ValidationException
from core.role_principals import RolePrincipal
from core.utils import current_schema, stable_hash

IDEMPOTENCY_KEY_MIN_LENGTH = 16
IDEMPOTENCY_KEY_MAX_LENGTH = 128


def validate_idempotency_key(raw: str | None) -> str:
    """Return an unmodified visible-ASCII key or a field-scoped 400.

    Whitespace is intentionally not trimmed: normalizing two byte-distinct client
    keys into one retry identity would make the contract ambiguous.
    """

    if (
        not isinstance(raw, str)
        or not IDEMPOTENCY_KEY_MIN_LENGTH <= len(raw) <= IDEMPOTENCY_KEY_MAX_LENGTH
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in raw)
    ):
        raise ValidationException(
            _("Idempotency-Key must contain 16 to 128 visible ASCII characters."),
            code="invalid_idempotency_key",
            fields={"Idempotency-Key": [_("Use 16 to 128 visible ASCII characters.")]},
        )
    return raw


def principal_scoped_key_hash(*, namespace: str, principal: RolePrincipal, raw: str) -> str:
    """Hash one key inside the exact tenant, domain, and role-principal namespace."""

    return stable_hash(
        f"idempotency-key:v1:{namespace}:{current_schema()}:{principal.kind}:{principal.principal_id}:{raw}"
    )


def operation_fingerprint(
    *,
    namespace: str,
    action: str,
    resource: Mapping[str, Any],
    body: Mapping[str, Any],
) -> str:
    """Hash a caller-supplied mutation after its DTO has been canonicalized."""

    payload = {"action": action, "body": body, "resource": resource}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return stable_hash(f"idempotency-operation:v1:{namespace}:{encoded}")


def lock_idempotency_key(*, namespace: str, principal: RolePrincipal, key_hash: str) -> None:
    """Serialize one exact principal/key before any domain or ledger mutation.

    The transaction-scoped PostgreSQL advisory lock lets a concurrent retry wait
    for the committed winner and return it.  The domain row's unique constraint is
    still the durable backstop.
    """

    lock_hash = stable_hash(
        f"idempotency-lock:v1:{namespace}:{current_schema()}:{principal.kind}:"
        f"{principal.principal_id}:{key_hash}"
    )
    # Fifteen hex digits fit safely inside PostgreSQL's signed bigint range.
    lock_id = int(lock_hash[:15], 16)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


def assert_principal_actor(
    *,
    actor: Any,
    principal: RolePrincipal,
    allowed_kinds: Collection[str],
) -> None:
    """Require the active role principal to resolve to the supplied bridge actor."""

    actor_id = getattr(actor, "pk", None)
    if (
        not isinstance(principal, RolePrincipal)
        or principal.kind not in allowed_kinds
        or isinstance(principal.principal_id, bool)
        or not isinstance(principal.principal_id, int)
        or principal.principal_id <= 0
        or isinstance(actor_id, bool)
        or not isinstance(actor_id, int)
        or actor_id <= 0
        or principal.user_id != actor_id
        or not bool(getattr(actor, "is_active", False))
    ):
        raise PermissionException(
            _("This money operation is unavailable for the active account session."),
            code="principal_unavailable",
        )
