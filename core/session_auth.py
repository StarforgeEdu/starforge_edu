"""Custom session authentication — no JWT library.

The opaque ``Session.key`` is the Bearer token. A session row lives in the tenant
schema that created it, so a key only authenticates against that center (tenant
binding is automatic — no signed claim to forge or check cross-schema). Validation
is one indexed lookup (key + not-revoked + not-expired); revocation is a row update.
Roles are read LIVE per request by the permission layer, so a role change takes
effect immediately — there is no stale-token window and no token_version dance.

Used by BOTH view styles during the migration:
- ``SessionAuthentication`` (DRF auth class) — swapped into REST_FRAMEWORK so every
  existing DRF endpoint authenticates by session key.
- ``core.api_auth.require_auth`` — the same validation for plain function views.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.authentication import BaseAuthentication, get_authorization_header

_LAST_USED_STALE = timedelta(seconds=60)


def _session_ttl() -> timedelta:
    return timedelta(days=int(getattr(settings, "SESSION_TTL_DAYS", 7)))


def create_session(
    user,
    *,
    ip: str = "",
    user_agent: str = "",
    device_id: str = "",
    read_only: bool = False,
    principal_kind: str = "",
    principal_id: int | None = None,
):
    """Issue a fresh session for ``user`` and return the row (``.key`` is the token).

    For role-native login, pass ``principal_kind`` (student/teacher/parent/staff) +
    ``principal_id`` (the role account's pk); ``user`` is still the account's linked User
    so all downstream authz/audit is unchanged."""
    from apps.users.models import Session

    return Session.objects.create(
        user=user,
        key=secrets.token_urlsafe(40),
        principal_kind=(principal_kind or "")[:16],
        principal_id=principal_id,
        ip_address=(ip or "")[:64],
        user_agent=(user_agent or "")[:512],
        device_id=(device_id or "")[:128],
        read_only=read_only,
        expires_at=timezone.now() + _session_ttl(),
    )


def validate_session_key(key: str):
    """Resolve a session key to its active session, or ``None``.

    Active = exists, not revoked, not expired, and the user is still active. Touches
    ``last_used_at`` at most once a minute (one cheap throttled UPDATE, no signals)."""
    from apps.users.models import Session

    if not key:
        return None
    session = (
        Session.objects.select_related("user")
        .filter(key=key, revoked_at__isnull=True, expires_at__gt=timezone.now())
        .first()
    )
    if session is None or not session.user.is_active:
        return None
    now = timezone.now()
    if (now - session.last_used_at) > _LAST_USED_STALE:
        Session.objects.filter(pk=session.pk).update(last_used_at=now)
    return session


def revoke_session(key: str) -> None:
    from apps.users.models import Session

    Session.objects.filter(key=key, revoked_at__isnull=True).update(revoked_at=timezone.now())


def revoke_all_for_user(user_id: int) -> int:
    """Revoke every active session for a user (logout-all / password change). Returns
    the number revoked."""
    from apps.users.models import Session

    return Session.objects.filter(user_id=user_id, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


class SessionAuthentication(BaseAuthentication):
    """DRF authenticator: ``Authorization: Bearer <session.key>`` -> (user, session).

    No header -> ``None`` (anonymous; ``IsAuthenticated`` then 401s). A Bearer key
    that is unknown/expired/revoked -> ``AuthenticationException`` (401)."""

    keyword = b"bearer"

    def authenticate(self, request):
        from core.exceptions import AuthenticationException

        header = get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword:
            return None
        if len(header) != 2:
            raise AuthenticationException(
                _("Invalid Authorization header."), code="authentication_failed"
            )
        try:
            key = header[1].decode()
        except UnicodeError:
            raise AuthenticationException(
                _("Invalid Authorization header."), code="authentication_failed"
            ) from None
        session = validate_session_key(key)
        if session is None:
            raise AuthenticationException(
                _("Your session is invalid or has expired. Please sign in again."),
                code="authentication_failed",
            )
        request.is_read_only_token = session.read_only
        # Role-native identity the caller signed in as (blank for legacy sessions).
        request.principal_kind = session.principal_kind
        request.principal_id = session.principal_id
        return session.user, session

    def authenticate_header(self, request) -> str:
        return "Bearer"
