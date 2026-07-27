"""Auth endpoints — plain Django function views (no DRF).

Each view: parse the JSON body -> build a DTO -> resolve the IAuthService from the
container -> return a success()/error() envelope. Auth is enforced by @require_auth
(custom session auth); rate limits by the @ratelimit decorator / check_rate helper.
Domain errors raised by the service are rendered as JSON by core.middleware.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.auth.dto.auth_dto import (
    ChangePasswordDTO,
    LoginDTO,
    ResetConfirmDTO,
    ResetRequestDTO,
    SessionContextDTO,
)
from apps.auth.interfaces.auth_service import IAuthService
from core.api_auth import deny_read_only_token, require_auth
from core.container import container
from core.http import read_json, str_field
from core.ratelimit import check_rate, ratelimit
from core.responses import no_content, success, validation_error
from core.utils import client_ip, user_agent


def _ctx(request: HttpRequest) -> SessionContextDTO:
    return SessionContextDTO(ip=client_ip(request), user_agent=user_agent(request))


def _service() -> IAuthService:
    # The container resolves the port to its bound concrete impl; mypy can't see the
    # binding, so the abstract-type warning is suppressed here (one place).
    return container.resolve(IAuthService)  # type: ignore[type-abstract]


@csrf_exempt
@require_POST
@ratelimit(limit=10, window=60, scope="login_ip")
def login_view(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    username = str_field(body, "username")
    password = str_field(body, "password")
    if not username or not password:
        return validation_error({"username": ["required"], "password": ["required"]})
    # Per-username cap (in addition to the per-IP decorator) — both 401s and successes
    # count, so credential stuffing one account is bounded. Keyed by tenant schema so a
    # flood of "admin" on one center never locks "admin" out on another.
    from core.utils import current_schema

    check_rate(scope="login_user", key=f"{current_schema()}:{username.strip().lower()}", limit=5, window=60)
    dto = LoginDTO(
        username=username,
        password=password,
        device_id=str_field(body, "device_id"),
        platform=str_field(body, "platform"),
    )
    return success(_service().login(dto, _ctx(request)))


@csrf_exempt
@require_POST
@ratelimit(limit=10, window=60, scope="login_ip")
def role_login_view(request: HttpRequest) -> HttpResponse:
    """Role-native login: a student/teacher/parent/staff signs in with their OWN role
    account's username+password (not a User). Same shape/limits as ``login_view``; returns
    ``{access, role, must_change_password}``. Additive — the legacy /login/ stays live."""
    body = read_json(request)
    username = str_field(body, "username")
    password = str_field(body, "password")
    if not username or not password:
        return validation_error({"username": ["required"], "password": ["required"]})
    from core.utils import current_schema

    check_rate(scope="login_user", key=f"{current_schema()}:{username.strip().lower()}", limit=5, window=60)
    dto = LoginDTO(
        username=username,
        password=password,
        device_id=str_field(body, "device_id"),
        platform=str_field(body, "platform"),
    )
    return success(_service().role_login(dto, _ctx(request)))


@csrf_exempt
@require_POST
@require_auth
def logout_view(request: HttpRequest) -> HttpResponse:
    deny_read_only_token(request)  # an impersonation session must not force-logout
    _service().logout(request.user)  # type: ignore[arg-type]
    return no_content()


@csrf_exempt
@require_POST
@require_auth
def password_change_view(request: HttpRequest) -> HttpResponse:
    deny_read_only_token(request)  # an impersonation session must not change the password
    body = read_json(request)
    dto = ChangePasswordDTO(
        old_password=str_field(body, "old_password"),
        new_password=str_field(body, "new_password"),
    )
    session = getattr(request, "auth", None)
    return success(
        _service().change_password(
            request.user,  # type: ignore[arg-type]
            dto,
            principal_kind=getattr(session, "principal_kind", ""),
            principal_id=getattr(session, "principal_id", None),
        )
    )


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
