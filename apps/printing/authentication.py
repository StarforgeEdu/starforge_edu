"""Branch-agent authentication (D4-LD-2).

A branch agent is NOT a ``users.User`` — it is a trusted daemon identified by a
hashed token. ``BranchAgentAuthentication`` reads ``Authorization: Agent <raw>``,
sha256-hashes the raw token, and looks up a non-revoked ``BranchAgent`` by
``token_hash``. On success ``request.auth`` is the ``BranchAgent`` and
``request.user`` stays anonymous (zero User involvement). Unknown / revoked /
malformed tokens raise a 401 ``agent_token_invalid`` envelope (TD-18).

The token hash is per-tenant-schema unique, and these views are reached through
the tenant URLConf (host resolves the schema), so an agent token only ever
authenticates inside its own tenant.
"""

from __future__ import annotations

from django.contrib.auth.models import AnonymousUser
from django.utils.translation import gettext_lazy as _
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import BasePermission

from core.exceptions import AuthenticationException
from core.utils import stable_hash

AGENT_AUTH_KEYWORD = "Agent"


class BranchAgentAuthentication(BaseAuthentication):
    """Authenticate ``Authorization: Agent <raw-token>`` against ``BranchAgent``."""

    keyword = AGENT_AUTH_KEYWORD

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header:
            return None  # let other authenticators / IsBranchAgent run

        parts = header.split()
        if not parts or parts[0] != self.keyword:
            # Empty/whitespace-only header or not an Agent token (e.g. a Bearer
            # JWT) — defer. Guarding `not parts` avoids an IndexError → 500 on a
            # whitespace-only Authorization header.
            return None
        if len(parts) != 2:
            raise AuthenticationException(_("Invalid agent token."), code="agent_token_invalid")

        from apps.printing.models import BranchAgent

        token_hash = stable_hash(parts[1])
        agent = (
            BranchAgent.objects.select_related("branch")
            .filter(token_hash=token_hash, revoked_at__isnull=True)
            .first()
        )
        if agent is None:
            raise AuthenticationException(_("Invalid agent token."), code="agent_token_invalid")

        return (AnonymousUser(), agent)

    def authenticate_header(self, request) -> str:
        return self.keyword


class IsBranchAgent(BasePermission):
    """Allow only requests carrying a valid ``BranchAgent`` in ``request.auth``."""

    def has_permission(self, request, view) -> bool:
        from apps.printing.models import BranchAgent

        return isinstance(getattr(request, "auth", None), BranchAgent)
