"""Users HTTP views (layered, off DRF).

The directory (list/retrieve at users:read), the self-scoped /me profile
(GET hydrate + PATCH self-service update), and the self-scoped device registry
(list/register/revoke, auth-only). Identity/device writes go through the
preserved apps.users.services domain functions via IUserService.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db.models import Prefetch, Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt

from apps.users.interfaces.services import IUserService
from apps.users.models import Device, RoleMembership, User
from apps.users.openapi_contracts import (
    ME_GET_CONTRACT,
    ME_HEAD_CONTRACT,
    ME_PATCH_CONTRACT,
    SESSION_DELETE_CONTRACT,
    SESSIONS_GET_CONTRACT,
    SESSIONS_HEAD_CONTRACT,
)
from apps.users.presenters import (
    device_to_dict,
    permission_context_to_dict,
    role_account_to_dict,
    session_to_dict,
    user_directory_row_to_dict,
    user_to_dict,
)
from core.api_auth import check_perm, deny_read_only_token, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import read_json, reject_unknown_fields, str_field
from core.listing import apply_filters, paginate, validate_pagination_filters
from core.openapi_contracts import openapi_contract
from core.permissions import get_user_roles
from core.responses import created, error, no_content, paginated, success
from core.scoping import is_permission_unscoped, permission_membership_scope_q
from core.utils import current_schema, user_agent

_GENDERS = frozenset(g[0] for g in User.Gender.choices)
_LANGUAGES = frozenset(lang[0] for lang in User.Language.choices)
_PLATFORMS = frozenset(p[0] for p in Device.PLATFORM_CHOICES)
_DIRECTORY_SEARCH = ("username", "first_name", "middle_name", "last_name", "phone")
_DIRECTORY_ORDERING = ("id", "username", "first_name", "last_name", "date_joined", "last_seen_at")
_ME_WRITABLE_FIELDS = frozenset(
    {
        "first_name",
        "last_name",
        "middle_name",
        "phone",
        "email",
        "birthdate",
        "gender",
        "preferred_language",
    }
)


def _service() -> IUserService:
    return container.resolve(IUserService)  # type: ignore[type-abstract]


def _organization_presentation_defaults() -> dict[str, str]:
    """Authoritative tenant-wide display defaults from CenterSettings."""
    from django.conf import settings as django_settings

    from apps.org.selectors import get_center_settings

    center_settings = get_center_settings()
    locale = center_settings.default_language or str(django_settings.LANGUAGE_CODE).split("-")[0]
    return {
        "organization_locale": locale,
        "organization_timezone": center_settings.organization_timezone,
        "primary_currency": center_settings.currency_primary,
    }


def _session_presentation(request: HttpRequest) -> dict[str, Any]:
    """Public current-session metadata without exposing the credential digest."""

    from core.session_auth import session_idle_timeout

    session = getattr(request, "auth", None)
    if session is None:
        return {}
    idle_expires_at = min(session.expires_at, session.last_used_at + session_idle_timeout())
    return {
        "session_id": session.pk,
        "session_created_at": session.created_at.isoformat(),
        "session_last_activity_at": session.last_used_at.isoformat(),
        "session_expires_at": session.expires_at.isoformat(),
        "session_idle_expires_at": idle_expires_at.isoformat(),
        "server_time": timezone.now().isoformat(),
    }


def _role_account(request: HttpRequest):
    """Return ``(kind, account)`` for a role session, else ``("", None)``."""
    session = getattr(request, "auth", None)
    kind = getattr(session, "principal_kind", "")
    account_id = getattr(session, "principal_id", None)
    if not kind or account_id is None:
        return "", None
    from apps.auth.services import _role_account_models
    from core.exceptions import AuthenticationException

    model = _role_account_models().get(kind)
    account = (
        model.objects.select_related("user")
        .prefetch_related(
            Prefetch(
                "user__role_memberships",
                queryset=RoleMembership.objects.select_related("account_type", "branch", "department"),
            )
        )
        .filter(pk=account_id, user=request.user)
        .first()
        if model
        else None
    )
    if account is None:
        raise AuthenticationException("Invalid account session.", code="authentication_failed")
    return kind, account


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require(data: dict[str, Any], name: str) -> Any:
    if name not in data or data[name] is None:
        raise _reject(name, "This field is required.")
    return data[name]


def _str_required(raw: Any, name: str, *, max_length: int) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    if "\x00" in raw:
        raise _reject(name, "Null characters are not allowed.")
    value = raw.strip()
    if not value:
        raise _reject(name, "This field may not be blank.")
    if len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _str_notnull(raw: Any, name: str, *, max_length: int, strip: bool = False) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    if "\x00" in raw:
        raise _reject(name, "Null characters are not allowed.")
    # DRF CharField trims surrounding whitespace by default (trim_whitespace=True)
    # before the length check — mirror that for the name/phone fields.
    value = raw.strip() if strip else raw
    if len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _choice(raw: Any, name: str, choices: frozenset[str]) -> str:
    # isinstance guard BEFORE the frozenset membership test (a list/dict would raise
    # an unhashable-type TypeError -> 500 instead of a clean 400).
    if not isinstance(raw, str) or raw not in choices:
        raise _reject(name, f"Must be one of: {', '.join(sorted(choices))}.")
    return raw


def _directory_query(request: HttpRequest) -> QuerySet[User]:
    """Return only users covered by the exact memberships granting ``users:read``.

    A branch-wide grant sees active assignments anywhere in that branch.  A
    department grant sees only active assignments at the same branch/department
    boundary.  Target memberships whose account type is inactive (or whose
    assignment is revoked) neither make a user visible nor appear in the detail
    payload.  Director/superuser authority remains organization-wide.

    Replacing the repository's general prefetch is security-significant: a user
    visible through Branch A may also hold a Branch B assignment, and that second
    assignment must not leak through ``role_memberships`` on the detail response.
    """
    queryset = _service().query()
    if is_permission_unscoped(request, permission="users:read"):
        return queryset

    visible_memberships = (
        RoleMembership.objects.filter(revoked_at__isnull=True)
        .filter(Q(account_type__isnull=True) | Q(account_type__is_active=True))
        .filter(
            permission_membership_scope_q(
                roles=get_user_roles(request),
                permission="users:read",
                branch_field="branch_id",
                department_field="department_id",
            )
        )
        .select_related("account_type", "branch", "department")
    )
    return (
        queryset.filter(role_memberships__in=visible_memberships)
        .distinct()
        .prefetch_related(None)
        .prefetch_related(Prefetch("role_memberships", queryset=visible_memberships))
    )


# --- /me self-service update ------------------------------------------------


def _me_changes(request: HttpRequest) -> dict[str, Any]:
    """Build a validated changes mapping for a legacy User-backed session."""
    data = read_json(request)
    reject_unknown_fields(data, allowed=_ME_WRITABLE_FIELDS)
    changes: dict[str, Any] = {}
    # NOT-NULL blank strings: reject explicit null, allow "", bounded at 150 (trimmed).
    for field in ("first_name", "last_name", "middle_name"):
        if field in data:
            changes[field] = _str_notnull(_reject_null(data[field], field), field, max_length=150, strip=True)
    # Nullable identifiers: null clears the column (null=True), else a bounded string.
    if "phone" in data:
        changes["phone"] = (
            None if data["phone"] is None else _str_notnull(data["phone"], "phone", max_length=32, strip=True)
        )
    if "email" in data:
        changes["email"] = _email_value(data["email"])
    # gender: NOT-NULL blank; "" or a valid choice.
    if "gender" in data:
        raw_gender = _reject_null(data["gender"], "gender")
        if not isinstance(raw_gender, str) or (raw_gender != "" and raw_gender not in _GENDERS):
            raise _reject("gender", f"Must be blank or one of: {', '.join(sorted(_GENDERS))}.")
        changes["gender"] = raw_gender
    # preferred_language: NOT-NULL choice (model default covers create, not touched here).
    if "preferred_language" in data:
        changes["preferred_language"] = _choice(
            _reject_null(data["preferred_language"], "preferred_language"),
            "preferred_language",
            _LANGUAGES,
        )
    # birthdate: nullable date.
    if "birthdate" in data:
        changes["birthdate"] = _date_value(data["birthdate"])
    return changes


def _reject_null(value: Any, name: str) -> Any:
    if value is None:
        raise _reject(name, "This field may not be null.")
    return value


def _email_value(raw: Any) -> str | None:
    if raw is None:  # email is null=True — an explicit null clears it.
        return None
    value = _str_notnull(raw, "email", max_length=254).strip()
    if value:
        try:
            validate_email(value)
        except DjangoValidationError:
            raise _reject("email", "Enter a valid email address.") from None
    return value


def _date_value(raw: Any) -> Any:
    if raw is None:  # birthdate is null=True.
        return None
    if not isinstance(raw, str):
        raise _reject("birthdate", "Date must be a string (YYYY-MM-DD).")
    try:
        parsed = parse_date(raw)
    except ValueError:  # a valid-format-but-impossible date (e.g. 2026-02-30).
        raise _reject("birthdate", "Enter a valid date (YYYY-MM-DD).") from None
    if parsed is None:
        raise _reject("birthdate", "Enter a valid date (YYYY-MM-DD).")
    return parsed


# --- views ------------------------------------------------------------------


@csrf_exempt
@require_auth
def users_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "users:read")
    qs = apply_filters(
        request,
        _directory_query(request),
        filter_fields=("is_active",),
        search_fields=_DIRECTORY_SEARCH,
        ordering_fields=_DIRECTORY_ORDERING,
        default_ordering="id",
    )
    items, total, page, size = paginate(request, qs)
    return paginated(
        [user_directory_row_to_dict(user) for user in items],
        total=total,
        page=page,
        page_size=size,
    )


@csrf_exempt
@require_auth
def user_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "users:read")
    user = _directory_query(request).filter(pk=pk).first()
    if user is None:
        raise NotFoundException(code="not_found")
    return success(user_to_dict(user))


@openapi_contract(
    path="/api/v1/users/me/",
    operations=(ME_GET_CONTRACT, ME_HEAD_CONTRACT, ME_PATCH_CONTRACT),
)
@csrf_exempt
@require_auth
def me_view(request: HttpRequest) -> HttpResponse:
    user: Any = request.user
    kind, account = _role_account(request)

    def payload() -> dict[str, Any]:
        if account is not None:
            from core.permissions import get_role_memberships

            profile = role_account_to_dict(
                kind,
                account,
                memberships=get_role_memberships(request),
            )
        else:
            hydrated_user = _service().get(user.pk) or user
            profile = user_to_dict(hydrated_user)
        profile.update(permission_context_to_dict(request))
        profile.update(_organization_presentation_defaults())
        profile.update(_session_presentation(request))
        profile["read_only_session"] = bool(getattr(getattr(request, "auth", None), "read_only", False))
        profile["tenant_slug"] = current_schema()
        return profile

    if request.method in ("GET", "HEAD"):
        return success(payload())
    if request.method == "PATCH":
        # Self-scoped write with no perm code -> reinstate the read-only-token deny
        # the old DenyWriteForReadOnlyToken gave (an impersonating admin must not
        # edit the target's profile).
        deny_read_only_token(request)
        if account is not None:
            body = read_json(request)
            reject_unknown_fields(body, allowed=_ME_WRITABLE_FIELDS)
            changes: dict[str, Any] = {}
            for field in ("first_name", "last_name", "middle_name"):
                if field in body:
                    changes[field] = _str_notnull(
                        _reject_null(body[field], field),
                        field,
                        max_length=150,
                        strip=True,
                    )
            if "phone" in body:
                changes["phone"] = (
                    ""
                    if body["phone"] is None
                    else _str_notnull(
                        body["phone"],
                        "phone",
                        max_length=32,
                        strip=True,
                    )
                )
            if "email" in body:
                email = _email_value(body["email"])
                changes["email"] = email or ""
            if "birthdate" in body:
                changes["birthdate"] = _date_value(body["birthdate"])
            if "gender" in body:
                raw_gender = _reject_null(body["gender"], "gender")
                choices = {choice for choice, _label in type(account).Gender.choices}
                if not isinstance(raw_gender, str) or (raw_gender and raw_gender not in choices):
                    raise _reject("gender", f"Must be blank or one of: {', '.join(sorted(choices))}.")
                changes["gender"] = raw_gender
            preferred_language = None
            if "preferred_language" in body:
                preferred_language = _choice(
                    _reject_null(body["preferred_language"], "preferred_language"),
                    "preferred_language",
                    _LANGUAGES,
                )
            from apps.users.services import update_role_identity

            update_role_identity(
                account,
                changes,
                preferred_language=preferred_language,
            )
            return success(payload())
        updated = _service().update_me(user=user, changes=_me_changes(request))
        profile = user_to_dict(_service().get(updated.pk) or updated)
        profile.update(permission_context_to_dict(request))
        profile.update(_organization_presentation_defaults())
        profile.update(_session_presentation(request))
        profile["read_only_session"] = bool(getattr(getattr(request, "auth", None), "read_only", False))
        profile["tenant_slug"] = current_schema()
        return success(profile)
    return _method_not_allowed()


def _session_principal(request: HttpRequest) -> tuple[str, int | None]:
    session = getattr(request, "auth", None)
    return (
        str(getattr(session, "principal_kind", "") or ""),
        getattr(session, "principal_id", None),
    )


def _validate_sessions_query(request: HttpRequest) -> None:
    allowed = {"page", "page_size"}
    unknown = sorted(set(request.GET) - allowed)
    duplicates = sorted(name for name in request.GET if len(request.GET.getlist(name)) != 1)
    if unknown or duplicates:
        fields = {
            name: ["Unknown query parameter." if name in unknown else "Supply this query parameter once."]
            for name in sorted(set(unknown) | set(duplicates))
        }
        raise ValidationException(
            "Invalid session-register query.",
            code="validation_error",
            fields=fields,
        )
    validate_pagination_filters(request)


@openapi_contract(
    path="/api/v1/users/sessions/",
    operations=(SESSIONS_GET_CONTRACT, SESSIONS_HEAD_CONTRACT),
)
@csrf_exempt
@require_auth
def sessions_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    _validate_sessions_query(request)
    principal_kind, principal_id = _session_principal(request)
    user: Any = request.user
    queryset = _service().sessions_for(
        user=user,
        principal_kind=principal_kind,
        principal_id=principal_id,
    )
    items, total, page, size = paginate(request, queryset)
    current_session_id = int(getattr(getattr(request, "auth", None), "pk", 0))
    return paginated(
        [session_to_dict(item, current_session_id=current_session_id) for item in items],
        total=total,
        page=page,
        page_size=size,
    )


@openapi_contract(
    path="/api/v1/users/sessions/{pk}/",
    operations=(SESSION_DELETE_CONTRACT,),
)
@csrf_exempt
@require_auth
def session_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "DELETE":
        return _method_not_allowed()
    deny_read_only_token(request)
    principal_kind, principal_id = _session_principal(request)
    user: Any = request.user
    if not _service().revoke_session(
        user=user,
        principal_kind=principal_kind,
        principal_id=principal_id,
        session_id=pk,
    ):
        raise NotFoundException(code="not_found")
    response = no_content()
    if (
        pk == getattr(getattr(request, "auth", None), "pk", None)
        and getattr(request, "auth_transport", "") == "cookie"
    ):
        from django.conf import settings

        response.delete_cookie(
            getattr(settings, "API_SESSION_COOKIE_NAME", "starforge_session"),
            samesite=getattr(settings, "API_SESSION_COOKIE_SAMESITE", "Lax"),
            path=getattr(settings, "API_SESSION_COOKIE_PATH", "/"),
        )
        response["Cache-Control"] = "no-store"
    return response


@csrf_exempt
@require_auth
def devices_collection_view(request: HttpRequest) -> HttpResponse:
    user: Any = request.user
    if request.method in ("GET", "HEAD"):
        items, total, page, size = paginate(request, _service().devices_for(user))
        return paginated([device_to_dict(d) for d in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        deny_read_only_token(request)
        data = read_json(request)
        device = _service().register_device(
            user=user,
            device_id=_str_required(_require(data, "device_id"), "device_id", max_length=128),
            platform=_choice(_require(data, "platform"), "platform", _PLATFORMS),
            user_agent=user_agent(request),
            push_token=str_field(data, "push_token"),
        )
        if device is None:  # defensive — validation above guarantees non-empty inputs.
            raise _reject("device_id", "This field is required.")
        return created(device_to_dict(device))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def device_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "DELETE":
        return _method_not_allowed()
    deny_read_only_token(request)
    user: Any = request.user
    if not _service().revoke_device(user=user, pk=pk):
        raise NotFoundException(code="not_found")
    return no_content()
