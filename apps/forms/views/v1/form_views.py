"""Forms / surveys endpoints — plain Django views over the layered architecture (F3-3/4).

Builders (forms:write) create/edit/publish/close forms + read responses/summary and
kick off AI analysis; anyone with forms:read sees published forms and submits a
response. Reads are ROW-scoped: a director sees the whole centre, a builder their own +
their branch(es), a responder only published forms in their branch or centre-wide.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.forms.dto.form_dto import AddFieldDTO, CreateFormDTO
from apps.forms.interfaces.services import IFormService
from apps.forms.models import Form, FormField
from apps.forms.presenters import form_to_dict, response_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import (
    Role,
    _request_overrides,
    get_role_memberships,
    get_user_roles,
    has_permission_code,
)
from core.responses import created, error, no_content, paginated, success

_RESOURCE = "forms"


def _service() -> IFormService:
    return container.resolve(IFormService)  # type: ignore[type-abstract]


def _scope(request: HttpRequest) -> tuple[bool, bool, set[int]]:
    """(is_unscoped, can_write, branch_ids) for the caller."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    is_unscoped = getattr(req.user, "is_superuser", False) or Role.DIRECTOR in roles
    can_write = has_permission_code(roles, f"{_RESOURCE}:write", _request_overrides(req))
    branch_ids = {m.branch_id for m in get_role_memberships(req) if m.branch_id}
    return is_unscoped, can_write, branch_ids


def _get_visible(request: HttpRequest, pk: int) -> Form:
    is_unscoped, can_write, branch_ids = _scope(request)
    form = _service().get_visible(
        user=request.user, is_unscoped=is_unscoped, can_write=can_write, branch_ids=branch_ids, pk=pk
    )
    if form is None:
        raise NotFoundException(code="not_found")  # not in the caller's scope -> 404, no leak
    return form


@csrf_exempt
@require_auth
def forms_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        is_unscoped, can_write, branch_ids = _scope(request)
        qs = _service().scoped_list(
            user=request.user, is_unscoped=is_unscoped, can_write=can_write, branch_ids=branch_ids
        )
        qs = apply_filters(
            request,
            qs,
            filter_fields=("status", "branch", "is_anonymous"),
            search_fields=("title",),
            ordering_fields=("created_at", "title"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([form_to_dict(f) for f in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def form_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    form = _get_visible(request, pk)
    if read:
        return success(form_to_dict(form))
    if request.method in ("PUT", "PATCH"):
        return success(form_to_dict(_service().update(form, _update_changes(read_json(request)))))
    if request.method == "DELETE":
        _service().delete(form)  # draft-only (422 otherwise)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def form_add_field_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    form = _get_visible(request, pk)
    field = _service().add_field(form, _field_dto(read_json(request)))
    from apps.forms.presenters import field_to_dict

    return created(field_to_dict(field))


@csrf_exempt
@require_auth
def form_publish_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    return success(form_to_dict(_service().publish(_get_visible(request, pk))))


@csrf_exempt
@require_auth
def form_close_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    return success(form_to_dict(_service().close(_get_visible(request, pk))))


@csrf_exempt
@require_auth
def form_submit_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")  # a responder submits
    form = _get_visible(request, pk)
    answers = _answers(read_json(request))
    response = _service().submit(form, respondent=request.user, answers=answers)
    # Anonymous-safe: only echo the receipt, never the respondent.
    return created({"id": response.id, "created_at": response.created_at.isoformat()})


@csrf_exempt
@require_auth
def form_responses_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    form = _get_visible(request, pk)
    items, total, page, size = paginate(request, _service().responses_of(form))
    return paginated([response_to_dict(r) for r in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def form_summary_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    return success(_service().summary(_get_visible(request, pk)))


@csrf_exempt
@require_auth
def form_analyze_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    form = _get_visible(request, pk)
    ai_request = _service().analyze(form, requested_by=request.user)
    # 202 Accepted — the narrative is produced async; poll /ai/requests/{id}/.
    return success({"request_id": ai_request.pk, "status": ai_request.status}, status=202)


# --- helpers ---------------------------------------------------------------
def _create(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    title = str_field(body, "title", max_length=200).strip()
    if not title:
        raise ValidationException(
            "Title is required.", code="validation_error", fields={"title": ["This field is required."]}
        )
    dto = CreateFormDTO(
        title=title,
        description=str_field(body, "description"),
        is_anonymous=bool_field(body, "is_anonymous"),
        allow_multiple=bool_field(body, "allow_multiple"),
        branch_id=int_field(body, "branch"),
        opens_at=_optional_datetime(body, "opens_at"),
        closes_at=_optional_datetime(body, "closes_at"),
        audience_roles=_audience_roles(body.get("audience_roles", [])),
        audience_user_ids=_audience_user_ids(body.get("audience_user_ids", [])),
    )
    is_unscoped, _can_write, branch_ids = _scope(request)
    form = _service().create(dto, creator=request.user, is_unscoped=is_unscoped, branch_ids=branch_ids)
    return created(form_to_dict(form))


def _update_changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "title" in body:
        title = str_field(body, "title", max_length=200).strip()
        if not title:
            raise ValidationException(
                "Title may not be blank.", code="validation_error", fields={"title": ["May not be blank."]}
            )
        changes["title"] = title
    if "description" in body:
        changes["description"] = str_field(body, "description")
    if "is_anonymous" in body:
        changes["is_anonymous"] = bool_field(body, "is_anonymous")
    if "allow_multiple" in body:
        changes["allow_multiple"] = bool_field(body, "allow_multiple")
    if "opens_at" in body:
        changes["opens_at"] = _optional_datetime(body, "opens_at")
    if "closes_at" in body:
        changes["closes_at"] = _optional_datetime(body, "closes_at")
    if "audience_roles" in body:
        changes["audience_roles"] = _audience_roles(body["audience_roles"])
    if "audience_user_ids" in body:
        changes["audience_user_ids"] = _audience_user_ids(body["audience_user_ids"])
    return changes


def _audience_roles(raw: Any) -> list[str]:
    """A deduped list of valid Role values the form targets (F3-2). A bad role is a 400."""
    if not isinstance(raw, list):
        raise ValidationException(
            "audience_roles must be a list.",
            code="validation_error",
            fields={"audience_roles": ["Must be a list of role names."]},
        )
    valid = set(Role.ALL)
    out: list[str] = []
    for role in raw:
        if not isinstance(role, str) or role not in valid:
            raise ValidationException(
                "Unknown role in audience_roles.",
                code="validation_error",
                fields={"audience_roles": [f"Unknown role: {role!r}."]},
            )
        if role not in out:
            out.append(role)
    return out


def _audience_user_ids(raw: Any) -> list[int]:
    """A deduped list of integer user ids the form targets (F3-2). A non-int is a 400."""
    if not isinstance(raw, list):
        raise ValidationException(
            "audience_user_ids must be a list.",
            code="validation_error",
            fields={"audience_user_ids": ["Must be a list of user ids."]},
        )
    out: list[int] = []
    for uid in raw:
        if not isinstance(uid, int) or isinstance(uid, bool):
            raise ValidationException(
                "audience_user_ids must be integers.",
                code="validation_error",
                fields={"audience_user_ids": ["Each id must be an integer."]},
            )
        if uid not in out:
            out.append(uid)
    return out


def _field_dto(body: dict[str, Any]) -> AddFieldDTO:
    label = str_field(body, "label", max_length=255)
    if not label:
        raise ValidationException(
            "Label is required.", code="validation_error", fields={"label": ["This field is required."]}
        )
    field_type = str_field(body, "field_type")
    if field_type not in FormField.FieldType.values:
        raise ValidationException(
            "Invalid field type.",
            code="validation_error",
            fields={"field_type": [f"Must be one of {', '.join(FormField.FieldType.values)}."]},
        )
    options = body.get("options", [])
    if not isinstance(options, list):
        raise ValidationException(
            "Options must be a list.", code="validation_error", fields={"options": ["Must be a list."]}
        )
    return AddFieldDTO(
        label=label,
        field_type=field_type,
        required=bool_field(body, "required"),
        order=int_field(body, "order"),
        options=options,
        help_text=str_field(body, "help_text", max_length=255),
    )


def _answers(body: dict[str, Any]) -> list[dict]:
    raw = body.get("answers")
    if not isinstance(raw, list):
        raise ValidationException(
            "answers must be a list of {field, value} objects.",
            code="validation_error",
            fields={"answers": ["Must be a list of {field, value} objects."]},
        )
    for item in raw:
        if not isinstance(item, dict) or "field" not in item:
            raise ValidationException(
                "each answer needs a 'field' id.",
                code="validation_error",
                fields={"answers": ["Each answer needs a 'field' id."]},
            )
        fid = item["field"]
        # The field id must be a scalar int — a list/dict id would be hashed against the
        # fields map downstream (fid in fields_by_id) and raise an unhashable-type
        # TypeError -> 500. Reject it here as a clean 400.
        if not isinstance(fid, int) or isinstance(fid, bool):
            raise ValidationException(
                "each answer 'field' must be an integer id.",
                code="validation_error",
                fields={"answers": ["Each answer 'field' must be an integer id."]},
            )
    return raw


def _optional_datetime(body: dict[str, Any], name: str):
    raw = body.get(name)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ValidationException(
            "Invalid datetime.", code="validation_error", fields={name: ["Must be an ISO 8601 datetime."]}
        )
    try:
        # parse_datetime RAISES ValueError on a regex-valid-but-impossible value.
        dt = parse_datetime(raw)
    except ValueError:
        dt = None
    if dt is None:
        raise ValidationException(
            "Invalid datetime.", code="validation_error", fields={name: ["Must be an ISO 8601 datetime."]}
        )
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt
