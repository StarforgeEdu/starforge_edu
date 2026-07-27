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
from django.http import HttpRequest, HttpResponse
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt

from apps.users.interfaces.services import IUserService
from apps.users.models import Device, User
from apps.users.presenters import device_to_dict, role_account_to_dict, user_to_dict
from core.api_auth import check_perm, deny_read_only_token, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import read_json, str_field
from core.listing import apply_filters, paginate
from core.responses import created, error, no_content, paginated, success
from core.utils import user_agent

_GENDERS = frozenset(g[0] for g in User.Gender.choices)
_LANGUAGES = frozenset(lang[0] for lang in User.Language.choices)
_PLATFORMS = frozenset(p[0] for p in Device.PLATFORM_CHOICES)
_TRUE = frozenset({"true", "1", "yes", "y", "t", "on"})
_FALSE = frozenset({"false", "0", "no", "n", "f", "off"})


def _service() -> IUserService:
    return container.resolve(IUserService)  # type: ignore[type-abstract]


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
        model.objects.select_related("user").filter(pk=account_id, user=request.user).first()
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


# --- /me self-service update ------------------------------------------------


def _me_changes(request: HttpRequest) -> dict[str, Any]:
    """Build the changes-dict for the writable UserSerializer fields present in the
    body. Read-only fields (username/is_staff/roles/date_joined/last_seen_at) are
    simply not read, so a PATCH attempting them is a no-op on them (parity)."""
    data = read_json(request)
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
    # is_active: NOT-NULL bool (strict DRF-parity coercion; reject null/garbage).
    if "is_active" in data:
        changes["is_active"] = _bool_value(_reject_null(data["is_active"], "is_active"))
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


def _bool_value(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
    raise _reject("is_active", "Must be a boolean.")


# --- views ------------------------------------------------------------------


@csrf_exempt
@require_auth
def users_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "users:read")
    qs = apply_filters(request, _service().query(), default_ordering="id")
    items, total, page, size = paginate(request, qs)
    return paginated([user_to_dict(u) for u in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def user_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "users:read")
    user = _service().get(pk)
    if user is None:
        raise NotFoundException(code="not_found")
    return success(user_to_dict(user))


@csrf_exempt
@require_auth
def me_view(request: HttpRequest) -> HttpResponse:
    user: Any = request.user
    kind, account = _role_account(request)
    if request.method in ("GET", "HEAD"):
        return success(role_account_to_dict(kind, account) if account is not None else user_to_dict(user))
    if request.method == "PATCH":
        # Self-scoped write with no perm code -> reinstate the read-only-token deny
        # the old DenyWriteForReadOnlyToken gave (an impersonating admin must not
        # edit the target's profile).
        deny_read_only_token(request)
        if account is not None:
            body = read_json(request)
            changes = {
                field: body[field]
                for field in ("first_name", "last_name", "middle_name", "phone", "email")
                if field in body
            }
            if "birthdate" in body:
                changes["birthdate"] = _date_value(body["birthdate"])
            if "gender" in body:
                raw_gender = _reject_null(body["gender"], "gender")
                choices = {choice for choice, _label in type(account).Gender.choices}
                if not isinstance(raw_gender, str) or (raw_gender and raw_gender not in choices):
                    raise _reject("gender", f"Must be blank or one of: {', '.join(sorted(choices))}.")
                changes["gender"] = raw_gender
            from apps.users.services import update_role_identity

            update_role_identity(account, changes)
            return success(role_account_to_dict(kind, account))
        updated = _service().update_me(user=user, changes=_me_changes(request))
        return success(user_to_dict(updated))
    return _method_not_allowed()


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
