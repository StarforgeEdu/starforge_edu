"""Channels middleware: resolve tenant from hostname + authenticate via JWT.

TD-1 for websockets: the token must be an *access* token, its ``schema`` claim
must match the host-resolved tenant, and its ``tv`` claim must equal the
user's current ``token_version`` (and the user must be active). Any failure
yields ``AnonymousUser`` — consumers (e.g. ``PingConsumer``) close 4401.

Schema handling: the user lookup runs via ``database_sync_to_async`` on
asgiref's thread-sensitive executor thread, whose thread-local DB connection
is independent of the event loop's. The schema switch therefore happens
*inside* ``_user_from_token`` via ``schema_context`` — setting the tenant on
the event-loop thread would never reach the thread that runs the query.

NOTE for Day-4 consumers: ``scope["tenant"]`` is plain scope state, it does
NOT set the connection schema. Any consumer doing DB work must wrap it in
``schema_context(scope["tenant"].schema_name)`` itself.
"""

from __future__ import annotations

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context


@database_sync_to_async
def _resolve_tenant_by_hostname(hostname: str):
    Tenant = get_tenant_model()
    try:
        return Tenant.objects.get(domains__domain=hostname)
    except Tenant.DoesNotExist:
        return None


@database_sync_to_async
def _user_from_token(raw_token: str, tenant):
    """Resolve a session-key Bearer token to its user, scoped to the host-resolved
    tenant's schema.

    Custom session auth (no JWT): the schema switch happens on THIS thread (asgiref's
    thread-sensitive executor, whose DB connection the event loop never touched), and
    the session lookup runs inside it. A cross-tenant key is simply not in this schema's
    Session table -> AnonymousUser (the consumer closes 4401). A revoked/expired session
    -> AnonymousUser. Roles are read live, so there is no token_version check."""
    from core.session_auth import validate_session_key

    expected_schema = tenant.schema_name if tenant is not None else get_public_schema_name()
    with schema_context(expected_schema):
        session = validate_session_key(raw_token)
        return session.user if session is not None else AnonymousUser()


class TenantAwareJWTAuthMiddleware(BaseMiddleware):
    """Resolves tenant by hostname; reads the session-key Bearer token from query
    string `token=` or the Sec-WebSocket-Protocol header (`bearer.<token>`).
    """

    async def __call__(self, scope, receive, send):
        host = ""
        for header_name, value in scope.get("headers", []):
            if header_name == b"host":
                host = value.decode().split(":")[0]
                break

        tenant = await _resolve_tenant_by_hostname(host) if host else None

        token = self._extract_token(scope)
        scope["user"] = await _user_from_token(token, tenant) if token else AnonymousUser()
        scope["tenant"] = tenant
        # Stash the raw session-key so the consumer can RE-validate it on each heartbeat
        # (R1-05): connect-time auth alone would let a force-logged-out / revoked session
        # keep receiving the live stream until it happens to disconnect.
        scope["_ws_token"] = token
        return await super().__call__(scope, receive, send)

    @staticmethod
    def _extract_token(scope) -> str | None:
        # 1) Sec-WebSocket-Protocol: bearer.<token>. ASGI servers parse the header
        #    into scope["subprotocols"]; read that first, then fall back to the raw
        #    header for environments that don't pre-parse it.
        for proto in scope.get("subprotocols", []) or []:
            if proto.startswith("bearer."):
                return proto.removeprefix("bearer.")
        for header_name, value in scope.get("headers", []):
            if header_name == b"sec-websocket-protocol":
                for part in value.decode().split(","):
                    part = part.strip()
                    if part.startswith("bearer."):
                        return part.removeprefix("bearer.")
        # 2) ?token=<token> — convenient for clients that can't set a subprotocol,
        #    but the token lands in proxy/access logs. Operators can disable this
        #    fallback (WEBSOCKET_ALLOW_QUERY_TOKEN=False) to force the subprotocol
        #    (bearer.<token>) path, which every browser can also use.
        from django.conf import settings

        if not getattr(settings, "WEBSOCKET_ALLOW_QUERY_TOKEN", True):
            return None
        query = parse_qs(scope.get("query_string", b"").decode())
        token_list = query.get("token") or []
        return token_list[0] if token_list else None


def public_schema_name() -> str:
    return get_public_schema_name()
