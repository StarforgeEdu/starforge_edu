"""SubscriptionGateMiddleware (D3-E-4) — the 402 paywall.

Sits at MIDDLEWARE index 1, immediately AFTER
`django_tenants.middleware.main.TenantMainMiddleware`, so `connection.tenant`
and `connection.schema_name` are already resolved when this runs.

Behavior:
- PUBLIC schema requests: no-op (platform admin + webhooks are unaffected).
- TENANT schema with subscription status `suspended`: respond 402 with the
  flat error envelope `{"success": false, "code": "subscription_required", "message": ...}`
  as a plain `JsonResponse` (this runs before DRF, so no exception handler).
- Allowlist prefixes always pass through (so a suspended tenant can still log
  in, reach /admin/, the health probes, and the schema): `/admin/`,
  `/api/v1/auth/`, `/healthz`, `/api/schema`.

The subscription status is looked up in the PUBLIC schema and cached for 60s in
Redis (see selectors.get_subscription_status) so this does not add a public
query to every tenant request.
"""

from __future__ import annotations

from django.db import connection
from django.http import JsonResponse
from django.utils.translation import gettext_lazy as _
from django_tenants.utils import get_public_schema_name

ALLOWLIST_PREFIXES = (
    "/admin/",
    "/api/v1/auth/",
    "/healthz",
    "/api/schema",
)


class SubscriptionGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        blocked = self._is_blocked(request)
        if blocked is not None:
            return blocked
        return self.get_response(request)

    def _is_blocked(self, request) -> JsonResponse | None:
        schema = getattr(connection, "schema_name", None)
        # Public schema (apex / webhooks / platform billing) is never gated.
        if not schema or schema == get_public_schema_name():
            return None

        path = request.path
        for prefix in ALLOWLIST_PREFIXES:
            if path.startswith(prefix):
                return None

        center = getattr(connection, "tenant", None)
        if center is None:
            return None

        # Lazy import: billing selectors touch the public-schema models; keeping
        # the import here avoids any import-time ordering issues during startup.
        from apps.billing.selectors import get_subscription_status

        status = get_subscription_status(schema_name=schema, center_id=center.pk)
        if status == "suspended":
            return JsonResponse(
                {
                    "success": False,
                    "code": "subscription_required",
                    "message": str(
                        _(
                            "This center's subscription is suspended. Please contact billing to restore access."
                        )
                    ),
                },
                status=402,
            )
        return None
