"""Auth endpoints — plain Django function views (no DRF).

Each view: parse the JSON body -> build a DTO -> resolve the IAuthService from the
container -> return a success()/error() envelope. Auth is enforced by @require_auth
(custom session auth); rate limits by the @ratelimit decorator / check_rate helper.
Domain errors raised by the service are rendered as JSON by core.middleware.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.auth.dto.auth_dto import (
    ChangePasswordDTO,
    LoginDTO,
    ResetConfirmDTO,
    ResetRequestDTO,
    SessionContextDTO,
)
from apps.auth.interfaces.auth_service import IAuthService
from apps.auth.openapi_contracts import (
    LOGOUT_ALL_CONTRACT,
    LOGOUT_CONTRACT,
    PASSWORD_CHANGE_CONTRACT,
    ROLE_LOGIN_CONTRACT,
    SESSION_BOOTSTRAP_CONTRACT,
)
from core.api_auth import deny_read_only_token, require_auth
from core.container import container
from core.http import read_json, str_field, trimmed_str_field
from core.openapi_contracts import openapi_contract
from core.ratelimit import check_rate, ratelimit
from core.responses import no_content, success, validation_error
from core.session_auth import SessionAuthentication, enforce_csrf
from core.utils import client_ip, user_agent

_COOKIE_TRANSPORT_HEADER = "cookie"


def _cookie_session_requested(request: HttpRequest) -> bool:
    return request.headers.get("X-Session-Transport", "").strip().lower() == _COOKIE_TRANSPORT_HEADER


def _cookie_csrf_protected(view_func):
    """Require CSRF before a browser-cookie login reaches the rate-limited view.

    Bearer/native callers retain the existing endpoint contract. The browser first
    visits ``session_bootstrap_view`` to receive the CSRF cookie and masked token.
    """

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if _cookie_session_requested(request):
            enforce_csrf(request)
        return view_func(request, *args, **kwargs)

    return wrapper


def _session_cookie_options() -> dict[str, Any]:
    return {
        "httponly": True,
        "secure": bool(getattr(settings, "API_SESSION_COOKIE_SECURE", True)),
        "samesite": getattr(settings, "API_SESSION_COOKIE_SAMESITE", "Lax"),
        "path": getattr(settings, "API_SESSION_COOKIE_PATH", "/"),
    }


def _browser_session_response(payload: dict[str, Any]) -> HttpResponse:
    """Move a newly issued raw key into an HttpOnly cookie and out of JSON."""

    session_key = str(payload.pop("access", ""))
    response = success(payload)
    if not session_key:
        return response
    response.set_cookie(
        getattr(settings, "API_SESSION_COOKIE_NAME", "starforge_session"),
        session_key,
        max_age=int(getattr(settings, "SESSION_TTL_DAYS", 7)) * 24 * 60 * 60,
        **_session_cookie_options(),
    )
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def _delete_browser_session(response: HttpResponse) -> HttpResponse:
    response.delete_cookie(
        getattr(settings, "API_SESSION_COOKIE_NAME", "starforge_session"),
        samesite=getattr(settings, "API_SESSION_COOKIE_SAMESITE", "Lax"),
        path=getattr(settings, "API_SESSION_COOKIE_PATH", "/"),
    )
    response["Cache-Control"] = "no-store"
    return response


def _logout_response(request: HttpRequest, *, message: str) -> HttpResponse:
    """Expire a browser cookie only after that cookie authenticated this request.

    An unauthenticated cross-site POST can still receive a response from this
    endpoint even though SameSite omitted the credential. Emitting an expiry in
    that response would let it clear an unrelated browser session. Bearer/native
    logout remains idempotent and never mutates the browser cookie jar.
    """

    response = success(message=message)
    if getattr(request, "auth_transport", "") == "cookie":
        return _delete_browser_session(response)
    return response


@openapi_contract(
    path="/api/v1/auth/session/",
    operations=(SESSION_BOOTSTRAP_CONTRACT,),
)
@require_GET
@ensure_csrf_cookie
def session_bootstrap_view(request: HttpRequest) -> HttpResponse:
    """Give same-origin browser clients a CSRF cookie before cookie login.

    The masked token is safe to expose to same-origin JavaScript and lets a login
    request proceed even when a privacy setting delays ``document.cookie`` updates.
    It is not an authentication credential.
    """

    response = success({"csrf_token": get_token(request)})
    response["Cache-Control"] = "no-store"
    return response


def _ctx(request: HttpRequest) -> SessionContextDTO:
    return SessionContextDTO(ip=client_ip(request), user_agent=user_agent(request))


def _service() -> IAuthService:
    # The container resolves the port to its bound concrete impl; mypy can't see the
    # binding, so the abstract-type warning is suppressed here (one place).
    return container.resolve(IAuthService)  # type: ignore[type-abstract]


def _logout_session(request: HttpRequest):
    """Return the live request session, or ``None`` for an already-ended session.

    Logout endpoints deliberately do not turn an absent, malformed, expired, or
    previously revoked credential into a new failure: the desired postcondition is
    already true. Authorization failures for a *live* restricted session still
    propagate (for example, read-only impersonation cannot invoke logout-all).
    """

    from core.exceptions import AuthenticationException

    try:
        result = SessionAuthentication().authenticate(request)
    except AuthenticationException:
        return None
    if result is None:
        return None
    request.user, request.auth = result  # type: ignore[attr-defined]
    return result[1]


@csrf_exempt
@require_POST
@_cookie_csrf_protected
@ratelimit(limit=10, window=60, scope="login_ip")
def login_view(request: HttpRequest) -> HttpResponse:
    # Generic bridge-User login is exclusively a public-schema control-center
    # operation. Tenant users authenticate through role-login so every session
    # is bound to one student/teacher/parent/staff principal.
    from django_tenants.utils import get_public_schema_name

    from core.exceptions import NotFoundException
    from core.utils import current_schema

    if current_schema() != get_public_schema_name():
        raise NotFoundException(code="not_found")
    body = read_json(request)
    username = trimmed_str_field(body, "username", max_length=150)
    password = str_field(body, "password", max_length=1024)
    if not username or not password:
        return validation_error({"username": ["required"], "password": ["required"]})
    # Per-username cap (in addition to the per-IP decorator) — both 401s and successes
    # count, so credential stuffing one account is bounded. Keyed by tenant schema so a
    # flood of "admin" on one center never locks "admin" out on another.
    check_rate(scope="login_user", key=f"{current_schema()}:{username.strip().lower()}", limit=5, window=60)
    dto = LoginDTO(
        username=username,
        password=password,
        device_id=str_field(body, "device_id", max_length=128),
        platform=str_field(body, "platform", max_length=32),
    )
    result = _service().login(dto, _ctx(request))
    if _cookie_session_requested(request):
        return _browser_session_response(result)
    return success(result)


@openapi_contract(
    path="/api/v1/auth/role-login/",
    operations=(ROLE_LOGIN_CONTRACT,),
)
@csrf_exempt
@require_POST
@_cookie_csrf_protected
@ratelimit(limit=10, window=60, scope="login_ip")
def role_login_view(request: HttpRequest) -> HttpResponse:
    """Role-native login: a student/teacher/parent/staff signs in with their OWN role
    account's username+password (not a User). Same shape/limits as ``login_view``; returns
    ``{access, role, must_change_password}``. Tenant URLConfs expose only this login;
    generic bridge-user login remains restricted to the public control-center schema."""
    body = read_json(request)
    username = trimmed_str_field(body, "username", max_length=150)
    password = str_field(body, "password", max_length=1024)
    if not username or not password:
        return validation_error({"username": ["required"], "password": ["required"]})
    from core.utils import current_schema

    check_rate(scope="login_user", key=f"{current_schema()}:{username.strip().lower()}", limit=5, window=60)
    dto = LoginDTO(
        username=username,
        password=password,
        device_id=str_field(body, "device_id", max_length=128),
        platform=str_field(body, "platform", max_length=32),
    )
    result = _service().role_login(dto, _ctx(request))
    if _cookie_session_requested(request):
        return _browser_session_response(result)
    return success(result)


@openapi_contract(
    path="/api/v1/auth/logout/",
    operations=(LOGOUT_CONTRACT,),
)
@csrf_exempt
@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    session = _logout_session(request)
    if session is not None:
        _service().logout(session)
    return _logout_response(request, message="Signed out.")


@openapi_contract(
    path="/api/v1/auth/logout-all/",
    operations=(LOGOUT_ALL_CONTRACT,),
)
@csrf_exempt
@require_POST
def logout_all_view(request: HttpRequest) -> HttpResponse:
    session = _logout_session(request)
    if session is not None:
        _service().logout_all(session.user)
    return _logout_response(request, message="Signed out on all devices.")


@openapi_contract(
    path="/api/v1/auth/password/change/",
    operations=(PASSWORD_CHANGE_CONTRACT,),
)
@csrf_exempt
@require_POST
@require_auth
def password_change_view(request: HttpRequest) -> HttpResponse:
    deny_read_only_token(request)  # an impersonation session must not change the password
    body = read_json(request)
    dto = ChangePasswordDTO(
        old_password=str_field(body, "old_password", max_length=1024),
        # Keep the raw, untrimmed value intact; the domain returns the stable
        # weak_password/new_password contract for every length outside 10..128.
        new_password=str_field(body, "new_password"),
    )
    session = getattr(request, "auth", None)
    ctx = _ctx(request)
    result = _service().change_password(
        request.user,  # type: ignore[arg-type]
        dto,
        principal_kind=getattr(session, "principal_kind", ""),
        principal_id=getattr(session, "principal_id", None),
        device_id=getattr(session, "device_id", ""),
        ip=ctx.ip,
        user_agent=ctx.user_agent,
    )
    if getattr(request, "auth_transport", "") == "cookie":
        return _browser_session_response(result)
    return success(result)


@csrf_exempt
@require_POST
@ratelimit(limit=10, window=60, scope="reset_ip")
def password_reset_request_view(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    dto = ResetRequestDTO(
        identifier=str_field(body, "identifier"),
        account_type=str_field(body, "account_type"),
    )
    _service().request_reset(dto, _ctx(request))
    # Always 202 whether or not an account matched (anti-enumeration).
    return success(message="If the account exists, a reset code has been sent.", status=202)


@csrf_exempt
@require_POST
@ratelimit(limit=10, window=60, scope="reset_confirm_ip")
def password_reset_confirm_view(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    dto = ResetConfirmDTO(
        identifier=str_field(body, "identifier"),
        code=str_field(body, "code"),
        new_password=str_field(body, "new_password"),
        account_type=str_field(body, "account_type"),
    )
    _service().confirm_reset(dto, _ctx(request))
    return no_content()
