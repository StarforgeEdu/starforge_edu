"""Project middleware.

Concerns, ordered in `config.settings.base.MIDDLEWARE`:

1. `RequestIDMiddleware` (outermost) — correlation id on every request/response.
2. `JsonErrorResponseMiddleware` — every error response is JSON, project-wide.
3. `HealthCheckMiddleware` (before tenant resolution) — liveness/readiness probes
   that answer on any Host header without a tenant.
4. `ApiRateLimitMiddleware` (before tenant resolution) — the blanket user/anon
   API rate limit for BOTH view styles (plain FBVs bypass DRF's throttles).
5. `InactiveTenantMiddleware` (after tenant resolution) — 503 on a deactivated
   Center (Lane B / D1-LB-6).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django_tenants.utils import get_public_schema_name

from core.logging_filters import request_id_var
from core.rate_config import RateConfigurationError, parse_rate
from core.utils import current_schema

REQUEST_ID_HEADER = "X-Request-ID"
logger = logging.getLogger("starforge.middleware")

# Inbound ids are attacker-controlled and end up in log lines (`req={request_id}`)
# and the response header — restrict to a safe charset and a sane length so a
# crafted value cannot forge/split log records or trigger BadHeaderError.
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PAYMENT_WEBHOOK_PATH_RE = re.compile(r"^/api/v1/webhooks/(?:click|payme|uzum)/[-A-Za-z0-9_]+/$")

GetResponse = Callable[[HttpRequest], HttpResponse]


class RequestIDMiddleware:
    """Honor a well-formed inbound ``X-Request-ID`` (charset/length validated) or
    mint a uuid4, expose it to the logging filters for the life of the request,
    and echo it on the response.
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = inbound if REQUEST_ID_RE.fullmatch(inbound) else uuid.uuid4().hex
        request.request_id = request_id  # type: ignore[attr-defined]
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        return response


class HealthCheckMiddleware:
    """Ops probes that bypass tenant resolution, auth, and throttling.

    - ``GET /healthz/live``  → 200 always (the process is serving).
    - ``GET /healthz/ready`` → 200 when Postgres + Redis answer, else 503 with the
      TD-18 error envelope (``code="not_ready"``).
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    _lock = threading.Lock()
    _probe_buckets: dict[str, tuple[float, int]] = {}
    _cached_ready: tuple[float, dict[str, object], int] | None = None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path == "/healthz/live":
            return JsonResponse({"status": "ok"})
        if request.path == "/healthz/ready":
            try:
                admitted = self._admit_probe(request)
            except RateConfigurationError:
                logger.critical("Invalid HEALTH_READY_RATELIMIT configuration.", exc_info=True)
                body, status = self._not_ready_result()
                return JsonResponse(body, status=status)
            if not admitted:
                response = JsonResponse(
                    {"success": False, "code": "throttled", "message": "Too many readiness probes."},
                    status=429,
                )
                response["Retry-After"] = "60"
                return response
            return self._ready()
        return self.get_response(request)

    @classmethod
    def _admit_probe(cls, request: HttpRequest) -> bool:
        from core.privacy import private_fingerprint
        from core.utils import client_ip

        limit, window = _parse_rate(
            getattr(settings, "HEALTH_READY_RATELIMIT", "30/min"),
            setting_name="HEALTH_READY_RATELIMIT",
        )
        ident = private_fingerprint(
            client_ip(request) or "unknown",
            namespace="health-ready-probe",
        )
        now = time.monotonic()
        with cls._lock:
            started, count = cls._probe_buckets.get(ident, (now, 0))
            if now - started >= window:
                started, count = now, 0
            count += 1
            cls._probe_buckets[ident] = (started, count)
            if len(cls._probe_buckets) > 4096:
                cls._probe_buckets.pop(next(iter(cls._probe_buckets)))
            return count <= limit

    @classmethod
    def _ready(cls) -> HttpResponse:
        ttl = float(getattr(settings, "HEALTH_READY_CACHE_SECONDS", 0))
        now = time.monotonic()
        with cls._lock:
            cached = cls._cached_ready
            if ttl > 0 and cached is not None and cached[0] > now:
                return JsonResponse(cached[1], status=cached[2])

        body, status = cls._readiness_result()
        if ttl > 0:
            with cls._lock:
                cls._cached_ready = (now + ttl, body, status)
        return JsonResponse(body, status=status)

    @staticmethod
    def _readiness_result() -> tuple[dict[str, object], int]:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception:
            logger.error("Readiness database probe failed.", exc_info=True)
            return HealthCheckMiddleware._not_ready_result()
        try:
            from infrastructure.cache.redis_client import get_redis

            redis = get_redis()
            redis.ping()
        except Exception:
            logger.error("Readiness cache probe failed.", exc_info=True)
            return HealthCheckMiddleware._not_ready_result()
        if getattr(settings, "HEALTH_REQUIRE_CELERY_HEARTBEAT", False):
            from celery_tasks.health_tasks import RUNTIME_HEARTBEAT_KEY

            try:
                if not redis.get(RUNTIME_HEARTBEAT_KEY):
                    raise RuntimeError("missing heartbeat")
            except Exception:
                logger.error("Readiness worker-heartbeat probe failed.", exc_info=True)
                return HealthCheckMiddleware._not_ready_result()
        return {"status": "ready"}, 200

    @staticmethod
    def _not_ready_result() -> tuple[dict[str, object], int]:
        """Return one public shape while component-level diagnostics stay in logs."""

        return {
            "success": False,
            "code": "not_ready",
            "message": "Service is not ready.",
        }, 503


class InactiveTenantMiddleware:
    """Return 503 ``center_inactive`` for a resolved-but-inactive Center.

    Runs after ``TenantMainMiddleware`` so the tenant is resolved. The public
    schema is never blocked, and the health probes already short-circuited
    earlier in the chain.
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        schema = getattr(connection, "schema_name", get_public_schema_name())
        if schema != get_public_schema_name():
            tenant = getattr(connection, "tenant", None)
            if tenant is not None and not getattr(tenant, "is_active", True):
                return JsonResponse(
                    {"success": False, "code": "center_inactive", "message": "This center is not active."},
                    status=503,
                )
        return self.get_response(request)


# ---------------------------------------------------------------------------
# Blanket API rate limit — both view styles (TD: keep 100k-user headroom sane)
# ---------------------------------------------------------------------------


def _parse_rate(rate: object, *, setting_name: str = "rate limit") -> tuple[int, int]:
    """Strict DRF-style rate parser retained as the middleware's public helper."""

    return parse_rate(rate, setting_name=setting_name)


def _rate_limit_unavailable_response() -> JsonResponse:
    """Render failures raised before the inner JSON-error middleware can run."""

    return JsonResponse(
        {
            "success": False,
            "code": "temporarily_unavailable",
            "message": "This operation is temporarily unavailable.",
        },
        status=503,
    )


class ApiRateLimitMiddleware:
    """Blanket request-rate cap for every ``/api/`` route, mirroring the DRF
    ``UserRateThrottle``/``AnonRateThrottle`` pair the migrated plain views no
    longer pass through (they bypass DRF dispatch entirely).

    Every request is first bucketed by client IP, regardless of the presented
    credential. Credential-free traffic also receives the stricter anonymous
    cap; Bearer- and cookie-present traffic is authenticated later and valid
    sessions receive a stable user-id cap. It
    sits before tenant resolution so a flood is rejected before it costs a schema
    lookup. OPTIONS preflights are exempt (CORS
    preflights never reached DRF's view-level throttles either). Endpoint-specific
    limits (login, bulk-import, OTP) still apply on top — the tighter bound wins.

    Rates come from ``settings.API_RATELIMIT_PREAUTH`` / ``API_RATELIMIT_ANON``
    (DRF-format strings, read lazily so ``override_settings`` works in tests).
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # The Django admin credential form (/admin/login/) is NOT under /api/, so it
        # bypasses the blanket limiter below — leaving it open to unlimited password
        # brute-force / credential-stuffing against staff & superuser accounts on
        # every tenant subdomain and the apex. Throttle the POST by client IP.
        if request.method == "POST" and request.path.endswith("/admin/login/"):
            from core.exceptions import ServiceUnavailableException, ThrottledException
            from core.ratelimit import check_rate
            from core.utils import client_ip

            ident = client_ip(request) or "anon"
            try:
                limit, window = _parse_rate(
                    getattr(settings, "ADMIN_LOGIN_RATELIMIT", "10/min"),
                    setting_name="ADMIN_LOGIN_RATELIMIT",
                )
                check_rate(scope="admin_login", key=ident, limit=limit, window=window)
            except RateConfigurationError:
                logger.critical("Invalid ADMIN_LOGIN_RATELIMIT configuration.", exc_info=True)
                return _rate_limit_unavailable_response()
            except ServiceUnavailableException:
                return _rate_limit_unavailable_response()
            except ThrottledException as exc:
                response = JsonResponse(
                    {"success": False, "code": exc.code, "message": str(exc.detail)}, status=429
                )
                response["Retry-After"] = str(int(exc.wait or window))
                return response

        # Payment-provider webhooks are unauthenticated at the
        # HTTP layer (signature-verified in the view) and pushed from the provider's
        # FIXED server IP(s). The blanket anon limiter runs BEFORE tenant resolution and
        # keys on client IP, so ALL tenants' callbacks for one provider collapse into a
        # single 60/min bucket — a payment burst 429s a provider callback (breaking
        # Payme's always-200 contract and causing provider-side cancel/retry), silently
        # desyncing the money path. Exempt them: they carry their own signature auth +
        # replay dedupe (WebhookEvent) + provider retry, so IP throttling is both
        # ineffective (spoofable) and harmful here.
        # Keep the exemption narrower than the URL namespace: otherwise arbitrary
        # or future routes below ``/webhooks/`` silently bypass every blanket cap.
        # Only the three registered POST callback shapes qualify.
        if request.method == "POST" and PAYMENT_WEBHOOK_PATH_RE.fullmatch(request.path):
            return self.get_response(request)

        if request.method != "OPTIONS" and request.path.startswith("/api/"):
            from core.exceptions import ServiceUnavailableException, ThrottledException
            from core.ratelimit import check_rate
            from core.utils import client_ip

            ident = client_ip(request) or "anon"
            try:
                # Always charge the source IP before tenant resolution. Random,
                # rotating Bearer strings can no longer mint fresh buckets.
                preauth_limit, preauth_window = _parse_rate(
                    getattr(settings, "API_RATELIMIT_PREAUTH", "300/min"),
                    setting_name="API_RATELIMIT_PREAUTH",
                )
                check_rate(
                    scope="api_pre_auth",
                    key=ident,
                    limit=preauth_limit,
                    window=preauth_window,
                )
                # Credential-free traffic also receives the tighter anonymous
                # cap. Recognized credential wire formats skip that duplicate
                # bucket, then valid sessions/devices get a stable principal
                # bucket after tenant resolution/authentication. Invalid Bearer,
                # cookie, and Agent values still pay the pre-auth IP cap above,
                # so rotating garbage credentials cannot mint unlimited buckets.
                auth = request.META.get("HTTP_AUTHORIZATION", "")
                bearer_present = auth[:7].lower() == "bearer " and bool(auth[7:].strip())
                agent_present = False
                if auth.startswith("Agent "):
                    from apps.printing.authentication import is_branch_agent_authorization

                    agent_present = is_branch_agent_authorization(auth)
                cookie_name = getattr(settings, "API_SESSION_COOKIE_NAME", "starforge_session")
                cookie_present = bool(request.COOKIES.get(cookie_name, "").strip())
                if not (bearer_present or agent_present or cookie_present):
                    anon_limit, anon_window = _parse_rate(
                        getattr(settings, "API_RATELIMIT_ANON", "60/min"),
                        setting_name="API_RATELIMIT_ANON",
                    )
                    check_rate(
                        scope="api_anon",
                        key=ident,
                        limit=anon_limit,
                        window=anon_window,
                    )
            except RateConfigurationError:
                logger.critical("Invalid API rate-limit configuration.", exc_info=True)
                return _rate_limit_unavailable_response()
            except ServiceUnavailableException:
                return _rate_limit_unavailable_response()
            except ThrottledException as exc:
                # Middleware-raised exceptions skip process_exception — render the
                # envelope directly (same shape the views produce).
                response = JsonResponse(
                    {"success": False, "code": exc.code, "message": str(exc.detail)}, status=429
                )
                response["Retry-After"] = str(int(exc.wait or 60))
                return response
        return self.get_response(request)


# ---------------------------------------------------------------------------
# JSON error envelope — project-wide (backend API: never serve an HTML error)
# ---------------------------------------------------------------------------

# Map an HTTP status to a stable, branchable error code (mirrors the DRF
# envelope in core.exceptions so API and non-API errors are indistinguishable).
_ERROR_CODES = {
    400: "bad_request",
    401: "authentication_failed",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    415: "unsupported_media_type",
    429: "throttled",
    500: "server_error",
    502: "bad_gateway",
    503: "service_unavailable",
}
_ERROR_DETAILS = {
    400: "Bad request.",
    401: "Authentication credentials were not provided or are invalid.",
    403: "You do not have permission to perform this action.",
    404: "Resource not found.",
    405: "Method not allowed.",
    429: "Too many requests.",
    500: "Internal server error.",
    503: "Service unavailable.",
}


def _error_envelope(status_code: int) -> dict[str, object]:
    # ONE flat error shape, byte-compatible with core.responses.error(), so a client
    # branches on the same `success`/`code`/`message` keys for EVERY error — a layered
    # domain error, a rate-limit 429, an unmatched-URL 404, an uncaught 500, or a 503
    # outage — instead of special-casing a nested {"error": {...}} for Django's own paths.
    return {
        "success": False,
        "code": _ERROR_CODES.get(status_code, "error"),
        "message": _ERROR_DETAILS.get(status_code, "An error occurred."),
    }


# ROOT_URLCONF / PUBLIC_SCHEMA_URLCONF handlerXXX — keep Django's own error
# responses (unmatched URL, uncaught 500, CSRF 403) as JSON, not HTML templates.
def json_404(request: HttpRequest, exception: object | None = None) -> JsonResponse:
    return JsonResponse(_error_envelope(404), status=404)


def json_400(request: HttpRequest, exception: object | None = None) -> JsonResponse:
    return JsonResponse(_error_envelope(400), status=400)


def json_403(request: HttpRequest, exception: object | None = None) -> JsonResponse:
    return JsonResponse(_error_envelope(403), status=403)


def json_500(request: HttpRequest) -> JsonResponse:
    return JsonResponse(_error_envelope(500), status=500)


class JsonErrorResponseMiddleware:
    """Guarantee every error response is JSON, project-wide.

    DRF endpoints already emit the flat ``{"success": false, "code", "message"}`` envelope
    via ``core.exceptions.drf_exception_handler``. This is the safety net for everything
    that does NOT pass through DRF — an unmatched URL, a non-DRF view, the admin, and
    (crucially) the DEBUG technical 404/500 pages — rewriting any HTML error response
    into the same envelope so an API/mobile client never receives an HTML page.

    Sits just below ``RequestIDMiddleware`` so it runs late on the way out: it MUTATES
    the response in place (never builds a new one), preserving headers inner middleware
    set — CORS, ``Retry-After`` — so a browser SPA can still read the error body.
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self._jsonify(self.get_response(request))

    def process_exception(self, request: HttpRequest, exc: Exception) -> HttpResponse | None:
        """Render a domain error raised by a plain (non-DRF) view as JSON, and as a
        defensive last resort map a leaked DB-level exception to a clean 4xx.

        DRF views handle ``StarforgeError`` inside their own exception handler; the
        layered function-based views let it propagate to here, where it becomes the
        ``{"success": false, code, message}`` envelope with the error's HTTP status.

        The off-DRF views also lost DRF's serializer validation, so a value that is
        too long / out of range / otherwise unstorable reaches the DB and raises a
        ``DataError``/``IntegrityError``. Those are NOT ``StarforgeError`` and would
        otherwise be a hard 500 (owner rule: bad input must never 500). Each statement
        runs in autocommit (no ATOMIC_REQUESTS), so the connection is still usable —
        render the honest 4xx here. Endpoint-level validation still gives better,
        field-specific messages; this is only the safety net for anything it misses."""
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.db import DataError, IntegrityError

        from core.exceptions import ConflictException, StarforgeError, ValidationException

        if not isinstance(exc, StarforgeError):
            if isinstance(exc, DataError):
                exc = ValidationException("A field value is invalid or too large.", code="invalid_input")
            elif isinstance(exc, IntegrityError):
                exc = ConflictException(
                    "The request conflicts with an existing record or a data constraint.",
                    code="conflict",
                )
            elif isinstance(exc, DjangoValidationError):
                # A layered service that runs Model.full_clean()/validate_constraints()
                # (e.g. a reversed date/time range violating a CheckConstraint) raises
                # Django's ValidationError — invalid input, not a server fault. Without
                # DRF's serializer layer it would otherwise be a hard 500. Surface the
                # per-field messages Django collected (message_dict) when it has them.
                try:
                    field_errors: dict | None = dict(exc.message_dict)
                except AttributeError:
                    field_errors = {"non_field_errors": list(exc.messages)}
                exc = ValidationException("Invalid input.", code="invalid_input", fields=field_errors)
            else:
                return None
        body: dict[str, object] = {"success": False, "code": exc.code, "message": str(exc.detail)}
        fields = getattr(exc, "fields", None)
        if fields:
            body["errors"] = fields
        response = JsonResponse(body, status=exc.status_code)
        wait = getattr(exc, "wait", None)
        if wait is not None:
            response["Retry-After"] = str(int(wait))
        return response

    @staticmethod
    def _jsonify(response: HttpResponse) -> HttpResponse:
        if getattr(response, "streaming", False) or response.status_code < 400:
            return response
        if "text/html" not in response.get("Content-Type", ""):
            return response  # already JSON (DRF) or a non-HTML body (e.g. a PDF)
        import json

        response.content = json.dumps(_error_envelope(response.status_code)).encode("utf-8")
        response["Content-Type"] = "application/json"
        # CommonMiddleware already stamped Content-Length from the ORIGINAL (longer) HTML body
        # — e.g. Django's DEBUG technical-404 page — so we MUST re-stamp it to the rewritten
        # JSON length. Otherwise the response declares more bytes than it sends: HTTP/2 aborts
        # the stream (ERR_HTTP2_PROTOCOL_ERROR) and HTTP/1.1 clients hang waiting for the rest.
        response["Content-Length"] = str(len(response.content))
        return response


class AppAvailabilityMiddleware:
    """Per-app fault isolation (see ``core.availability``).

    A disabled app — or one whose HARD dependency is down — answers a clean
    ``503 service_unavailable`` JSON, so ONE app going down never takes the rest of the API
    with it. An app running DEGRADED (a SOFT dependency down) is served normally but its JSON
    success envelope gains a structured, executive-safe ``warnings`` entry. Dependency names
    remain available through the operator system-status endpoint rather than leaking into
    ordinary user-facing responses. Only touches
    ``/api/v1/<mount>/`` routes; ``admin``/``schema``/health and unmanaged paths pass through.
    """

    _API_PREFIX = "/api/v1/"

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        outcome = self._resolve(request)
        if isinstance(outcome, HttpResponse):  # 503 short-circuit for a down app
            return outcome
        response = self.get_response(request)
        if outcome:  # a non-empty warnings list -> the app is degraded
            self._inject_warnings(response, outcome)
        return response

    def _resolve(self, request: HttpRequest):
        """Return a 503 short circuit, structured degradation warnings, or ``None``.

        ``resolve_status`` deliberately retains detailed dependency strings for the
        authenticated operator status surface. Normal API responses receive a stable public
        warning DTO so internal app labels and outage topology are not exposed to end users.
        """
        # The public control plane has no tenant-local CenterSettings table. In
        # particular, /api/v1/auth/login/ is mounted on both URLconfs; consulting
        # tenant availability while serving it on the apex aborts the surrounding
        # transaction before the login view can query the public User table.
        if current_schema() == get_public_schema_name():
            return None

        from core.availability import (
            STATUS_DISABLED,
            STATUS_UNAVAILABLE,
            app_for_mount,
            resolve_status,
        )

        path = request.path
        if not path.startswith(self._API_PREFIX):
            return None
        mount = path[len(self._API_PREFIX) :].split("/", 1)[0]
        app = app_for_mount(mount)
        if app is None:
            return None
        status, warnings = resolve_status(app)
        if status in (STATUS_DISABLED, STATUS_UNAVAILABLE):
            logger.warning(
                "Capability unavailable for app %s with status %s: %s",
                app,
                status,
                warnings,
            )
            return JsonResponse(
                {
                    "success": False,
                    "code": "service_unavailable",
                    "message": "This capability is temporarily unavailable.",
                },
                status=503,
            )
        if not warnings:
            return None
        return [
            {
                "code": "information_delayed",
                "message": "Some information may be delayed.",
                "affected_sections": [mount],
            }
        ]

    @staticmethod
    def _inject_warnings(response: HttpResponse, warnings: list[dict[str, object]]) -> None:
        """Merge warnings into a JSON success envelope without clobbering view warnings.

        Error, streaming, non-JSON, and malformed success responses remain untouched. Existing
        warnings win on duplicate ``(code, affected_sections)`` identities so a view can supply
        a more specific message than the middleware fallback.
        """
        if getattr(response, "streaming", False) or response.status_code >= 400:
            return
        if "application/json" not in response.get("Content-Type", ""):
            return
        import json

        try:
            body = json.loads(response.content)
        except (ValueError, TypeError):
            return
        if not isinstance(body, dict) or body.get("success") is not True:
            return
        existing = body.get("warnings", [])
        if not isinstance(existing, list):
            return

        def warning_identity(value: object) -> tuple[str, tuple[str, ...]] | None:
            if not isinstance(value, dict):
                return None
            code = value.get("code")
            sections = value.get("affected_sections")
            if not isinstance(code, str) or not isinstance(sections, list):
                return None
            if not sections or not all(isinstance(section, str) for section in sections):
                return None
            return code, tuple(sections)

        merged = list(existing)
        identities: set[tuple[str, tuple[str, ...]]] = set()
        for existing_warning in existing:
            if identity := warning_identity(existing_warning):
                identities.add(identity)
        for warning in warnings:
            identity = warning_identity(warning)
            if identity is not None and identity not in identities:
                merged.append(warning)
                identities.add(identity)
        body["warnings"] = merged
        response.content = json.dumps(body).encode("utf-8")
        response["Content-Length"] = str(len(response.content))


class OrganizationTimezoneMiddleware:
    """Activate authoritative organization business time for one tenant request.

    The context manager owns public-schema bypass and restoration, including when
    downstream middleware or a view raises, so worker threads cannot leak one tenant's
    timezone into the next request.
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from core.exceptions import ServiceUnavailableException
        from core.timezones import organization_timezone_context

        try:
            with organization_timezone_context():
                return self.get_response(request)
        except ServiceUnavailableException as exc:
            # Exceptions raised while entering middleware context do not reach
            # Django's process_exception hooks. Render the one expected domain
            # failure here so missing/invalid organization time policy is a
            # stable fail-closed 503 rather than a leaked test-client exception.
            return JsonResponse(
                {
                    "success": False,
                    "code": exc.code,
                    "message": str(exc.detail),
                },
                status=exc.status_code,
            )
