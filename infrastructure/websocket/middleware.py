"""Fail-closed tenant resolution and session authentication for WebSockets.

Browser credentials are accepted from a ``bearer.<opaque-key>`` subprotocol or
the host-only HttpOnly API cookie. Production rejects query-string credentials.
The raw key exists only for the handshake lookup and is never retained in the
ASGI scope; live consumers revalidate the resulting tenant-local session id.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import MultipleObjectsReturned
from django.http import parse_cookie
from django.http.request import split_domain_port, validate_host
from django_tenants.utils import get_tenant_model, schema_context

from core.privacy import private_fingerprint

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{32,256}\Z")
_MAX_HOST_BYTES = 255
_MAX_ORIGIN_BYTES = 512
_MAX_PROTOCOL_BYTES = 1024
_MAX_COOKIE_BYTES = 8192
_MAX_QUERY_BYTES = 2048
_CLOSE_UNAUTHORIZED = 4401
_CLOSE_FORBIDDEN = 4403
_CLOSE_RATE_LIMITED = 4429


@dataclass(frozen=True, slots=True)
class _Credential:
    token: str = ""
    transport: str = ""
    invalid: bool = False


def _header_values(scope, name: bytes) -> list[bytes]:
    return [value for header_name, value in scope.get("headers", []) if header_name.lower() == name]


def _canonical_host(scope) -> str | None:
    """Return one ALLOWED_HOSTS-approved hostname, without a port."""

    values = _header_values(scope, b"host")
    if len(values) != 1 or len(values[0]) > _MAX_HOST_BYTES:
        return None
    try:
        raw_host = values[0].decode("ascii")
    except UnicodeError:
        return None
    domain, _port = split_domain_port(raw_host)
    domain = domain.lower().rstrip(".")
    if not domain or domain.startswith("[") or not validate_host(domain, settings.ALLOWED_HOSTS):
        return None
    return domain


def _normalized_origin(value: str) -> tuple[str, str, int] | None:
    """Parse one exact HTTP(S) origin into a comparison tuple."""

    try:
        parsed = urlsplit(value)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError):
        return None
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.scheme, hostname, port


def _origin_allowed(scope, host: str) -> tuple[bool, bool]:
    """Return ``(allowed, present)`` for a browser Origin header.

    Native clients may omit Origin. Any client that sends it must match the
    target origin or an exact, explicitly configured development/frontend
    origin. Wildcards are deliberately unsupported.
    """

    values = _header_values(scope, b"origin")
    if not values:
        return True, False
    if len(values) != 1 or len(values[0]) > _MAX_ORIGIN_BYTES:
        return False, True
    try:
        raw_origin = values[0].decode("ascii")
    except UnicodeError:
        return False, True
    origin = _normalized_origin(raw_origin)
    if origin is None:
        return False, True

    scheme = "https" if scope.get("scheme") == "wss" else "http"
    host_values = _header_values(scope, b"host")
    try:
        target = _normalized_origin(f"{scheme}://{host_values[0].decode('ascii')}")
    except (IndexError, UnicodeError):
        target = None
    if target is not None and origin == target:
        return True, True

    configured = {
        normalized
        for raw in getattr(settings, "WEBSOCKET_ALLOWED_ORIGINS", [])
        if (normalized := _normalized_origin(str(raw).strip())) is not None
    }
    return origin in configured, True


def _valid_token(value: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(value))


def _extract_credential(scope, *, origin_present: bool, origin_allowed: bool) -> _Credential:
    """Extract exactly one unambiguous credential from the ASGI handshake."""

    raw_query = scope.get("query_string", b"")
    if not isinstance(raw_query, bytes) or len(raw_query) > _MAX_QUERY_BYTES:
        return _Credential(invalid=True)
    try:
        query = parse_qs(raw_query.decode("ascii"), keep_blank_values=True, max_num_fields=32)
    except (UnicodeError, ValueError):
        return _Credential(invalid=True)
    token_values = query.get("token", [])
    # Reject a URL credential even when a stronger transport is also present:
    # accepting the socket would normalize leaking the extra key into access
    # logs, history, and telemetry.
    # URL credentials are never accepted in any environment.  A development
    # escape hatch is too easy to inherit in staging and still copies secrets
    # into browser history, reverse-proxy access logs, and error telemetry.
    if token_values:
        return _Credential(invalid=True)
    if len(token_values) > 1:
        return _Credential(invalid=True)

    raw_headers = _header_values(scope, b"sec-websocket-protocol")
    if len(raw_headers) > 1 or any(len(value) > _MAX_PROTOCOL_BYTES for value in raw_headers):
        return _Credential(invalid=True)
    protocols = list(scope.get("subprotocols", []) or [])
    header_protocols: list[str] = []
    if raw_headers:
        try:
            header_protocols = [part.strip() for part in raw_headers[0].decode("ascii").split(",")]
        except UnicodeError:
            return _Credential(invalid=True)
    if protocols and header_protocols and protocols != header_protocols:
        return _Credential(invalid=True)
    if not protocols:
        protocols = header_protocols
    if len(protocols) > 16 or any(not isinstance(item, str) or len(item) > 300 for item in protocols):
        return _Credential(invalid=True)
    if any(not item for item in protocols) or len(set(protocols)) != len(protocols):
        return _Credential(invalid=True)
    bearer_values = [item.removeprefix("bearer.") for item in protocols if item.startswith("bearer.")]
    if len(bearer_values) > 1:
        return _Credential(invalid=True)
    if bearer_values:
        if token_values:
            return _Credential(invalid=True)
        token = bearer_values[0]
        return _Credential(token=token, transport="subprotocol", invalid=not _valid_token(token))

    cookie_headers = _header_values(scope, b"cookie")
    if len(cookie_headers) > 1 or any(len(value) > _MAX_COOKIE_BYTES for value in cookie_headers):
        return _Credential(invalid=True)
    if cookie_headers:
        try:
            raw_cookie = cookie_headers[0].decode("latin1")
            cookies = parse_cookie(raw_cookie)
        except UnicodeError:
            return _Credential(invalid=True)
        cookie_name = getattr(settings, "API_SESSION_COOKIE_NAME", "starforge_session")
        cookie_occurrences = sum(
            1 for part in raw_cookie.split(";") if part.partition("=")[0].strip() == cookie_name
        )
        if cookie_occurrences > 1:
            return _Credential(invalid=True)
        token = cookies.get(cookie_name, "").strip()
        if token:
            if token_values:
                return _Credential(invalid=True)
            # Cookies are ambient credentials: unlike native Bearer clients,
            # they always require a valid browser Origin.
            if not origin_present or not origin_allowed or not _valid_token(token):
                return _Credential(invalid=True)
            return _Credential(token=token, transport="cookie")

    return _Credential()


def _client_address(scope) -> str:
    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        return str(client[0])[:64]
    return "unknown"


def _scrub_credentials(scope) -> None:
    """Remove every credential-bearing transport value before app dispatch."""

    scope["subprotocols"] = [
        protocol
        for protocol in scope.get("subprotocols", []) or []
        if isinstance(protocol, str) and not protocol.startswith("bearer.")
    ]
    clean_headers: list[tuple[bytes, bytes]] = []
    for name, value in scope.get("headers", []):
        lowered = name.lower()
        if lowered == b"cookie":
            continue
        if lowered == b"sec-websocket-protocol":
            try:
                safe = [
                    part.strip()
                    for part in value.decode("ascii").split(",")
                    if part.strip() and not part.strip().startswith("bearer.")
                ]
            except UnicodeError:
                continue
            if safe:
                clean_headers.append((name, ", ".join(safe).encode("ascii")))
            continue
        clean_headers.append((name, value))
    scope["headers"] = clean_headers
    scope["query_string"] = b""


@sync_to_async
def _rate_limited(*, scope: str, key: str, limit: int, window: int = 60) -> bool:
    from core.exceptions import ThrottledException
    from core.ratelimit import check_rate

    try:
        check_rate(scope=scope, key=key, limit=limit, window=window)
    except ThrottledException:
        return True
    except Exception:
        logger.warning("WebSocket rate-limit dependency failed.", exc_info=True)
        return True
    return False


@database_sync_to_async
def _resolve_tenant_by_hostname(hostname: str):
    Tenant = get_tenant_model()
    try:
        return Tenant.objects.get(
            domains__domain__iexact=hostname,
            is_active=True,
            archived_at__isnull=True,
        )
    except (Tenant.DoesNotExist, MultipleObjectsReturned):
        return None


@database_sync_to_async
def _session_from_key(raw_token: str, tenant):
    """Authenticate once inside the host-resolved tenant schema."""

    from core.session_auth import session_requires_password_change, validate_session_key

    try:
        with schema_context(tenant.schema_name):
            session = validate_session_key(raw_token)
            if session is None:
                return AnonymousUser(), False, None, False, "", None, False
            password_change_required = session_requires_password_change(session)
            user = AnonymousUser() if password_change_required else session.user
            return (
                user,
                password_change_required,
                session.pk,
                bool(session.read_only),
                session.principal_kind,
                session.principal_id,
                False,
            )
    except Exception as exc:
        # Do not attach a traceback from this frame to telemetry: its argument is
        # the live bearer key. Preserve only the exception class for operations.
        raw_token = ""
        logger.warning("WebSocket session dependency failed (%s).", type(exc).__name__)
        return AnonymousUser(), False, None, False, "", None, True


async def _close(send, code: int) -> None:
    await send({"type": "websocket.close", "code": code})


class TenantAwareAuthMiddleware(BaseMiddleware):
    """Validate host/origin/rate, resolve one tenant, then authenticate."""

    async def __call__(self, scope, receive, send):
        # Work on a shallow copy so security scrubbing does not mutate state
        # owned by an outer ASGI server/middleware implementation.
        scope = dict(scope)
        scope["headers"] = list(scope.get("headers", []))
        host = _canonical_host(scope)
        if host is None:
            await _close(send, _CLOSE_FORBIDDEN)
            return

        origin_allowed, origin_present = _origin_allowed(scope, host)
        if not origin_allowed:
            await _close(send, _CLOSE_FORBIDDEN)
            return

        client_ref = private_fingerprint(_client_address(scope), namespace="ws-handshake-ip")
        preauth_limit = int(getattr(settings, "WEBSOCKET_HANDSHAKE_RATE_LIMIT", 120))
        if await _rate_limited(scope="ws_handshake", key=client_ref, limit=preauth_limit):
            await _close(send, _CLOSE_RATE_LIMITED)
            return

        credential = _extract_credential(
            scope,
            origin_present=origin_present,
            origin_allowed=origin_allowed,
        )
        if credential.invalid:
            await _close(send, _CLOSE_UNAUTHORIZED)
            return
        if not credential.token:
            await _close(send, _CLOSE_UNAUTHORIZED)
            return

        try:
            tenant = await _resolve_tenant_by_hostname(host)
        except Exception as exc:
            _scrub_credentials(scope)
            credential = _Credential(invalid=True)
            logger.warning("WebSocket tenant resolution failed (%s).", type(exc).__name__)
            await _close(send, 1011)
            return
        if tenant is None:
            await _close(send, _CLOSE_UNAUTHORIZED)
            return

        user = AnonymousUser()
        password_change_required = False
        session_id = None
        read_only = False
        principal_kind = ""
        principal_id = None
        session_dependency_failed = False
        if credential.token:
            try:
                (
                    user,
                    password_change_required,
                    session_id,
                    read_only,
                    principal_kind,
                    principal_id,
                    session_dependency_failed,
                ) = await _session_from_key(credential.token, tenant)
            except Exception as exc:
                _scrub_credentials(scope)
                credential = _Credential(invalid=True)
                logger.warning("WebSocket session resolution failed (%s).", type(exc).__name__)
                await _close(send, 1011)
                return
        if session_dependency_failed:
            _scrub_credentials(scope)
            credential = _Credential(invalid=True)
            await _close(send, 1011)
            return

        if session_id is not None:
            user_limit = int(getattr(settings, "WEBSOCKET_USER_CONNECT_RATE_LIMIT", 30))
            user_key = f"{tenant.schema_name}:{user.pk}:{session_id}"
            if await _rate_limited(scope="ws_user_connect", key=user_key, limit=user_limit):
                await _close(send, _CLOSE_RATE_LIMITED)
                return

        _scrub_credentials(scope)
        scope["user"] = user
        scope["tenant"] = tenant
        scope["password_change_required"] = password_change_required
        scope["read_only_session"] = read_only
        scope["principal_kind"] = principal_kind
        scope["principal_id"] = principal_id
        scope["principal_validated"] = session_id is not None
        scope["_ws_session_id"] = session_id
        scope["_ws_auth_transport"] = credential.transport
        return await super().__call__(scope, receive, send)
