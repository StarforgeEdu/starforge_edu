"""Per-(schema, user) throttles for expensive authenticated endpoints.

These guard cost-heavy actions (mass messaging, synchronous CSV import, AI
generation) that the broad ``user`` rate (1000/min) does not meaningfully bound.
Rates live in ``REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']`` (config/settings/base).
"""

from __future__ import annotations

from rest_framework.throttling import UserRateThrottle

from core.utils import current_schema


class _ScopedUserThrottle(UserRateThrottle):
    """Throttle keyed by (schema, user) so one tenant's user can't exhaust another
    tenant's pk-colliding bucket on the shared cache, and falls back to IP for the
    (rare) anonymous case."""

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            ident = f"{current_schema()}:{user.pk}"
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class AnnouncementThrottle(_ScopedUserThrottle):
    scope = "announcement"


class BulkImportThrottle(_ScopedUserThrottle):
    scope = "bulk_import"


class AIGenerationThrottle(_ScopedUserThrottle):
    scope = "ai_generation"
