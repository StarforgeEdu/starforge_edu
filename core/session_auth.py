"""Custom session authentication — no JWT library.

The opaque key returned by :func:`create_session` is the Bearer token. Only a
one-way SHA-256 digest is stored on ``Session``. A session row lives in the tenant
schema that created it, so a key only authenticates against that center (tenant
binding is automatic — no signed claim to forge or check cross-schema). Validation
is one indexed digest lookup (plus a temporary legacy-key fallback while old rows
are upgraded); revocation is a row update.
Roles are read LIVE per request by the permission layer, so a role change takes
effect immediately — there is no stale-token window and no token_version dance.

Used by BOTH view styles during the migration:
- ``SessionAuthentication`` (DRF auth class) — swapped into REST_FRAMEWORK so every
  existing DRF endpoint authenticates by session key. Browser clients may send the
  same key in a Secure, HttpOnly, SameSite cookie; unsafe cookie-authenticated
  requests are centrally CSRF checked even while legacy views remain csrf-exempt for
  non-browser Bearer clients.
- ``core.api_auth.require_auth`` — the same validation for plain function views.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework.authentication import BaseAuthentication, CSRFCheck, get_authorization_header

_LAST_USED_STALE = timedelta(seconds=60)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ROLE_MODEL_LABELS = {
    "student": "students.StudentProfile",
    "teacher": "teachers.TeacherProfile",
    "parent": "parents.ParentProfile",
    "staff": "org.StaffProfile",
}
_SESSION_HASH_PREFIX = "sha256$"
_MAX_SESSION_KEY_LENGTH = 256
# An identity-only capability, deliberately not representable by request input or
# a caller-provided boolean.  It is attached only after this module has performed
# the live profile ownership check in ``validate_session_key``.
_VALIDATED_ROLE_PRINCIPAL_MARKER = object()
_PASSWORD_CHANGE_ALLOWED_REQUESTS = {
    "/api/v1/users/me/": frozenset({"GET", "HEAD", "OPTIONS"}),
    "/api/v1/auth/password/change/": frozenset({"POST", "OPTIONS"}),
    "/api/v1/auth/logout/": frozenset({"POST", "OPTIONS"}),
    "/api/v1/auth/logout-all/": frozenset({"POST", "OPTIONS"}),
}


def enforce_csrf(request) -> None:
    """Apply Django's CSRF validation and raise the project-standard JSON 403.

    Most API views intentionally remain ``csrf_exempt`` because native clients use
    Bearer credentials. Cookie authentication cannot inherit that exemption: this
    explicit check runs inside the authenticator, before any protected view body, and
    therefore covers both DRF and plain-Django endpoints in one place.
    """

    from core.exceptions import PermissionException

    def dummy_get_response(_request):  # pragma: no cover - middleware constructor hook
        return HttpResponse()

    check = CSRFCheck(dummy_get_response)
    check.process_request(request)
    reason = check.process_view(request, dummy_get_response, (), {})
    if reason:
        raise PermissionException(
            _("Your security check expired. Refresh the page and try again."),
            code="csrf_failed",
        )


def hash_session_key(key: str) -> str:
    """Return the non-reversible representation persisted for a Bearer key.

    Session keys contain 320 bits of CSPRNG entropy, so a fast digest is suitable:
    unlike a human password there is no feasible dictionary to brute-force. The
    prefix makes stored digests unambiguous and, critically, prevents a digest
    copied from the database from being accepted by the legacy plaintext fallback.
    """
    from core.utils import stable_hash

    return f"{_SESSION_HASH_PREFIX}{stable_hash(key)}"


def _looks_like_stored_session_hash(value: str) -> bool:
    digest = value.removeprefix(_SESSION_HASH_PREFIX)
    return (
        value.startswith(_SESSION_HASH_PREFIX)
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    )


def _session_ttl() -> timedelta:
    return timedelta(days=int(getattr(settings, "SESSION_TTL_DAYS", 7)))


def session_idle_timeout() -> timedelta:
    """Enforced inactivity window shared by authentication and bootstrap data."""

    return timedelta(minutes=int(getattr(settings, "SESSION_IDLE_TIMEOUT_MINUTES", 8 * 60)))


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
    """Issue a fresh session and return its row (``.key`` is available once).

    For role-native login, pass ``principal_kind`` (student/teacher/parent/staff) +
    ``principal_id`` (the role account's pk); ``user`` is still the account's linked User
    so all downstream authz/audit is unchanged. The raw key is attached only to
    this in-memory instance; database reads expose ``key_hash`` and never recover it.
    """
    from apps.users.models import Session

    raw_key = secrets.token_urlsafe(40)
    session = Session.objects.create(
        user=user,
        key_hash=hash_session_key(raw_key),
        principal_kind=(principal_kind or "")[:16],
        principal_id=principal_id,
        ip_address=(ip or "")[:64],
        user_agent=(user_agent or "")[:512],
        device_id=(device_id or "")[:128],
        read_only=read_only,
        expires_at=timezone.now() + _session_ttl(),
    )
    session._issued_key = raw_key
    return session


def session_validated_request_principal(request) -> bool:
    """Whether this exact request passed the live role-session validation gate."""

    return getattr(request, "_role_principal_validation_marker", None) is _VALIDATED_ROLE_PRINCIPAL_MARKER


def validate_session_key(key: str):
    """Resolve a session key to its active session, or ``None``.

    Active = exists, not revoked, not expired, and the user is still active. Touches
    ``last_used_at`` at most once a minute (one cheap throttled UPDATE, no signals)."""
    from apps.users.models import Session

    if not key or len(key) > _MAX_SESSION_KEY_LENGTH:
        return None
    now = timezone.now()
    key_hash = hash_session_key(key)
    session = (
        Session.objects.select_related("user")
        .filter(key_hash=key_hash, revoked_at__isnull=True, expires_at__gt=now)
        .first()
    )
    # Safe dual-read during rollout: old rows may still contain their raw key.
    # Never run this branch for a value shaped like a stored digest, otherwise a
    # read-only database leak would itself become a usable Bearer credential.
    if session is None and not _looks_like_stored_session_hash(key):
        session = (
            Session.objects.select_related("user")
            .filter(key_hash=key, revoked_at__isnull=True, expires_at__gt=now)
            .first()
        )
        if session is not None:
            Session.objects.filter(pk=session.pk, key_hash=key).update(key_hash=key_hash)
            session.key_hash = key_hash
    return _validate_loaded_session(session, now=now, touch=True)


def validate_session_id(
    session_id: int,
    *,
    expected_user_id: int | None = None,
    touch: bool = True,
):
    """Resolve an already-authenticated session by its tenant-local row id.

    WebSocket authentication uses the raw credential exactly once during the
    handshake, then retains only this non-secret identifier in the ASGI scope.
    This prevents exception/debug scope dumps from copying a live bearer key.
    The caller must already have resolved the tenant schema and should supply
    the user id established by that handshake as an additional consistency
    check. Passive transports pass ``touch=False`` so heartbeats do not defeat
    the interactive-session idle timeout.
    """

    from apps.users.models import Session

    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
        return None
    now = timezone.now()
    sessions = Session.objects.select_related("user").filter(
        pk=session_id,
        revoked_at__isnull=True,
        expires_at__gt=now,
    )
    if expected_user_id is not None:
        sessions = sessions.filter(user_id=expected_user_id)
    return _validate_loaded_session(sessions.first(), now=now, touch=touch)


def _validate_loaded_session(session, *, now, touch: bool):
    """Apply live idle, account, and principal checks to one loaded session."""

    if session is None:
        return None
    if now - session.last_used_at >= session_idle_timeout():
        type(session).objects.filter(pk=session.pk, revoked_at__isnull=True).update(revoked_at=now)
        return None
    if not session.user.is_active or not _has_live_role_principal(session):
        # Persist the invalidation so old blank tenant sessions and deleted role
        # principals cannot be retried on every request or WebSocket handshake.
        type(session).objects.filter(pk=session.pk, revoked_at__isnull=True).update(revoked_at=now)
        return None
    if touch and (now - session.last_used_at) > _LAST_USED_STALE:
        type(session).objects.filter(pk=session.pk).update(last_used_at=now)
        session.last_used_at = now
    return session


def _has_live_role_principal(session) -> bool:
    """Validate the role identity represented by a role-native session.

    The linked ``User`` is only an authorization bridge.  It is insufficient proof that
    the student/teacher/parent/staff account still exists or remains active, and a bridge
    with Django-admin privileges must never be accepted on the role session surface.
    """
    kind = session.principal_kind
    principal_id = session.principal_id
    session._principal_must_change_password = False
    if not kind and principal_id is None:
        from django.conf import settings
        from django_tenants.utils import get_public_schema_name

        from core.utils import current_schema

        if current_schema() == get_public_schema_name():
            return bool(session.user.is_staff or session.user.is_superuser)
        # Explicitly test-only compatibility while legacy fixtures move to
        # role-native sessions. Staging/production force this setting off.
        return bool(getattr(settings, "ALLOW_LEGACY_TENANT_SESSIONS_FOR_TESTS", False))
    model_label = _ROLE_MODEL_LABELS.get(kind)
    if model_label is None or principal_id is None:
        return False

    from django.apps import apps as django_apps

    model = django_apps.get_model(model_label)
    account = (
        model.objects.filter(pk=principal_id).only("is_active", "user_id", "must_change_password").first()
    )
    is_live = bool(
        account is not None
        and account.is_active
        and account.user_id == session.user_id
        and not session.user.is_staff
        and not session.user.is_superuser
    )
    if is_live:
        session._principal_must_change_password = bool(account.must_change_password)
    return is_live


def session_requires_password_change(session) -> bool:
    """Whether this role-native session is restricted by a temporary password.

    ``validate_session_key`` populates the value from the same live principal query
    used to validate the account, so normal HTTP and WebSocket authentication does
    not pay for a second query. The fallback keeps this helper safe for Session
    instances loaded through another repository path.
    """

    cached = getattr(session, "_principal_must_change_password", None)
    if cached is not None:
        return bool(cached)
    kind = getattr(session, "principal_kind", "")
    principal_id = getattr(session, "principal_id", None)
    model_label = _ROLE_MODEL_LABELS.get(kind)
    if model_label is None or principal_id is None:
        return False

    from django.apps import apps as django_apps

    model = django_apps.get_model(model_label)
    return bool(
        model.objects.filter(pk=principal_id, user_id=session.user_id)
        .values_list("must_change_password", flat=True)
        .first()
    )


def enforce_password_change_policy(session, path: str, method: str) -> None:
    """Deny business access until a role account replaces its temporary password."""

    allowed_methods = _PASSWORD_CHANGE_ALLOWED_REQUESTS.get(path, frozenset())
    if session_requires_password_change(session) and method.upper() not in allowed_methods:
        from core.exceptions import PermissionException

        raise PermissionException(
            _("You must change your password before continuing."),
            code="password_change_required",
        )


def revoke_session(key: str) -> None:
    from apps.users.models import Session

    if not key or len(key) > _MAX_SESSION_KEY_LENGTH:
        return
    candidates = [hash_session_key(key)]
    if not _looks_like_stored_session_hash(key):
        candidates.append(key)  # legacy plaintext row during rollout
    Session.objects.filter(key_hash__in=candidates, revoked_at__isnull=True).update(revoked_at=timezone.now())


def revoke_all_for_user(user_id: int) -> int:
    """Revoke every active session for a user (logout-all / password change). Returns
    the number revoked."""
    from apps.users.models import Session

    return Session.objects.filter(user_id=user_id, revoked_at__isnull=True).update(revoked_at=timezone.now())


class SessionAuthentication(BaseAuthentication):
    """Resolve a Bearer header or the browser's HttpOnly session cookie.

    An explicit Bearer header always wins, preserving the non-browser client contract.
    Cookie-authenticated unsafe methods additionally require Django's CSRF cookie/header
    pair. No credential -> ``None``; unknown/expired/revoked credentials -> 401.
    """

    keyword = b"bearer"

    def authenticate(self, request):
        from core.exceptions import AuthenticationException

        header = get_authorization_header(request).split()
        transport = "bearer"
        if header:
            if header[0].lower() != self.keyword or len(header) != 2:
                raise AuthenticationException(
                    _("Invalid Authorization header."), code="authentication_failed"
                )
            try:
                key = header[1].decode()
            except UnicodeError:
                raise AuthenticationException(
                    _("Invalid Authorization header."), code="authentication_failed"
                ) from None
        else:
            cookie_name = getattr(settings, "API_SESSION_COOKIE_NAME", "starforge_session")
            key = request.COOKIES.get(cookie_name, "")
            if not key:
                return None
            transport = "cookie"

        session = validate_session_key(key)
        if session is None:
            raise AuthenticationException(
                _("Your session is invalid or has expired. Please sign in again."),
                code="authentication_failed",
            )
        if transport == "cookie" and request.method not in _SAFE_METHODS:
            enforce_csrf(request)
        path = getattr(request, "path_info", request.path)
        enforce_password_change_policy(session, path, request.method)
        if session.read_only and request.method not in _SAFE_METHODS and path != "/api/v1/auth/logout/":
            # Enforce read-only impersonation centrally at authentication time.  This
            # covers DRF and the layered plain-Django views because both call this same
            # authenticator; individual views no longer need to remember the guard.
            # Ending only the impersonation session is safe and remains available.
            from core.exceptions import PermissionException

            raise PermissionException(code="read_only_token")
        request.is_read_only_token = session.read_only
        request.auth_transport = transport
        # Role-native identity the caller signed in as (blank for legacy sessions).
        request.principal_kind = session.principal_kind
        request.principal_id = session.principal_id
        if (
            not session.principal_kind
            and session.principal_id is None
            and getattr(settings, "ALLOW_LEGACY_PRINCIPAL_UNION_FOR_TESTS", False) is True
        ):
            # Test suites still contain compatibility fixtures that mint a blank
            # tenant session for a plain User. The permission layer accepts that
            # union only through this server-owned marker; request input can never
            # opt in, while staging and production force the setting off.
            request._allow_legacy_principal_union_for_tests = True
        # Permission resolution may trust this identity without repeating the
        # live role-account query already performed by validate_session_key().
        request.principal_validated = True
        request._role_principal_validation_marker = _VALIDATED_ROLE_PRINCIPAL_MARKER
        # Model signals fire after authentication but have no request argument;
        # publish the live principal to the request-local audit context.
        from apps.audit.context import bind_actor

        bind_actor(session.user)
        return session.user, session

    def authenticate_header(self, request) -> str:
        return "Bearer"
