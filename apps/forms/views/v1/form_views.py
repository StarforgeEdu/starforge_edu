"""Forms / surveys endpoints — plain Django views over the layered architecture (F3-3/4).

Builders (forms:write) create/edit/publish/close forms + read responses/summary and
kick off AI analysis; anyone with forms:read sees published forms and submits a
response. Reads are ROW-scoped: a director sees the whole centre, a builder their own +
their branch(es), a responder only published forms in their branch or centre-wide.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.forms.dto.form_dto import AddFieldDTO, CreateFormDTO
from apps.forms.interfaces.services import IFormService
from apps.forms.models import Form, FormField
from apps.forms.openapi_contracts import (
    FORM_ADD_FIELD_CONTRACT,
    FORM_ANALYZE_CONTRACT,
    FORM_CLOSE_CONTRACT,
    FORM_DETAIL_CONTRACTS,
    FORM_PUBLISH_CONTRACT,
    FORM_RESPONSES_CONTRACTS,
    FORM_SUBMIT_CONTRACT,
    FORM_SUMMARY_CONTRACTS,
    FORMS_COLLECTION_CONTRACTS,
)
from apps.forms.presenters import form_to_dict, response_to_dict
from apps.forms.services import (
    MAX_CHOICE_OPTION_CHARS,
    MAX_CHOICE_OPTIONS,
    MAX_FORM_DESCRIPTION_CHARS,
    MAX_FORM_FIELDS,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, parse_bool, read_json, str_field
from core.listing import apply_filters, paginate, positive_int_filter, validate_pagination_filters
from core.openapi_contracts import openapi_contract
from core.permissions import (
    Role,
    _request_overrides,
    get_user_roles,
    has_permission_code,
)
from core.responses import created, error, no_content, paginated, success
from core.role_principals import RolePrincipal, request_role_principal
from core.scoping import is_permission_unscoped, permission_membership_scopes

_RESOURCE = "forms"
_CREATE_FIELDS = frozenset(
    {
        "title",
        "description",
        "is_anonymous",
        "allow_multiple",
        "branch",
        "opens_at",
        "closes_at",
        "audience_roles",
        "audience_user_ids",
    }
)
_UPDATE_FIELDS = _CREATE_FIELDS - {"branch"}
_FIELD_FIELDS = frozenset({"label", "field_type", "required", "order", "options", "help_text"})


class _Scope(NamedTuple):
    read_unscoped: bool
    write_unscoped: bool
    can_write: bool
    roles: set[str]
    principal: RolePrincipal
    read_branch_ids: set[int]
    write_branch_ids: set[int]

    def can_manage(self, form: Form) -> bool:
        return self.write_unscoped or (form.branch_id is not None and form.branch_id in self.write_branch_ids)


def _service() -> IFormService:
    return container.resolve(IFormService)  # type: ignore[type-abstract]


def _request_principal(request: HttpRequest) -> RolePrincipal:
    cached = getattr(request, "_forms_role_principal", None)
    if isinstance(cached, RolePrincipal):
        return cached
    principal = request_role_principal(request, error_code="forms_principal_unavailable")
    request._forms_role_principal = principal  # type: ignore[attr-defined]
    return principal


def _scope(request: HttpRequest) -> _Scope:
    """Permission-paired read/write branch scopes for the caller."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    principal = _request_principal(request)
    superuser = bool(getattr(req.user, "is_superuser", False))
    read_scopes = permission_membership_scopes(roles=roles, permission=f"{_RESOURCE}:read")
    write_scopes = permission_membership_scopes(roles=roles, permission=f"{_RESOURCE}:write")
    return _Scope(
        read_unscoped=superuser or is_permission_unscoped(req, permission=f"{_RESOURCE}:read"),
        write_unscoped=superuser or is_permission_unscoped(req, permission=f"{_RESOURCE}:write"),
        can_write=superuser or has_permission_code(roles, f"{_RESOURCE}:write", _request_overrides(req)),
        roles=roles,
        principal=principal,
        # Form has no department attribution, so a department-only grant cannot be
        # expanded to every form in its branch.
        read_branch_ids={scope.branch_id for scope in read_scopes if scope.department_id is None},
        write_branch_ids={scope.branch_id for scope in write_scopes if scope.department_id is None},
    )


def _get_visible(request: HttpRequest, pk: int) -> Form:
    scope = _scope(request)
    form = _service().get_visible(
        user=request.user,
        read_unscoped=scope.read_unscoped,
        write_unscoped=scope.write_unscoped,
        can_write=scope.can_write,
        roles=scope.roles,
        principal_kind=scope.principal.kind,
        principal_id=scope.principal.principal_id,
        read_branch_ids=scope.read_branch_ids,
        write_branch_ids=scope.write_branch_ids,
        pk=pk,
    )
    if form is None:
        raise NotFoundException(code="not_found")  # not in the caller's scope -> 404, no leak
    return form


def _get_manageable(request: HttpRequest, pk: int) -> Form:
    """Resolve the narrower scope for lifecycle and response-management actions.

    A branch-scoped builder may read and answer a published centre-wide form, but
    that read leg must not let them edit/close it or inspect all respondents.
    """
    form = _get_visible(request, pk)
    if _scope(request).can_manage(form):
        return form
    raise NotFoundException(code="not_found")


@openapi_contract(
    path="/api/v1/forms/",
    operations=FORMS_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def forms_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:read")
        scope = _scope(request)
        qs = _service().scoped_list(
            user=request.user,
            read_unscoped=scope.read_unscoped,
            write_unscoped=scope.write_unscoped,
            can_write=scope.can_write,
            roles=scope.roles,
            principal_kind=scope.principal.kind,
            principal_id=scope.principal.principal_id,
            read_branch_ids=scope.read_branch_ids,
            write_branch_ids=scope.write_branch_ids,
        )
        _validate_filters(request)
        qs = apply_filters(
            request,
            qs,
            filter_fields=("status", "branch", "is_anonymous"),
            search_fields=("title",),
            ordering_fields=("created_at", "title"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated(
            [form_to_dict(f, include_management=scope.can_manage(f)) for f in items],
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        _validate_query(request, allowed=set())
        return _create(request, body=read_json(request))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/forms/{pk}/",
    operations=FORM_DETAIL_CONTRACTS,
)
@csrf_exempt
@require_auth
def form_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD", "PUT", "PATCH", "DELETE"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    _validate_query(request, allowed=set())
    form = _get_visible(request, pk) if read else _get_manageable(request, pk)
    if read:
        return success(form_to_dict(form, include_management=_scope(request).can_manage(form)))
    if request.method in ("PUT", "PATCH"):
        body = _body(read_json(request), allowed=_UPDATE_FIELDS)
        if request.method == "PATCH" and not body:
            raise ValidationException(
                "Provide at least one field to update.",
                code="validation_error",
                fields={"body": ["Provide at least one field to update."]},
            )
        return success(
            form_to_dict(
                _service().update(
                    form,
                    _update_changes(
                        body,
                        partial=request.method == "PATCH",
                    ),
                )
            )
        )
    if request.method == "DELETE":
        _service().delete(form)  # draft-only (422 otherwise)
        return no_content()
    raise AssertionError("unreachable")


@openapi_contract(
    path="/api/v1/forms/{pk}/fields/",
    operations=(FORM_ADD_FIELD_CONTRACT,),
)
@csrf_exempt
@require_auth
def form_add_field_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _validate_query(request, allowed=set())
    form = _get_manageable(request, pk)
    field = _service().add_field(
        form,
        _field_dto(_body(read_json(request), allowed=_FIELD_FIELDS)),
    )
    from apps.forms.presenters import field_to_dict

    return created(field_to_dict(field))


@openapi_contract(
    path="/api/v1/forms/{pk}/publish/",
    operations=(FORM_PUBLISH_CONTRACT,),
)
@csrf_exempt
@require_auth
def form_publish_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _validate_query(request, allowed=set())
    _body(read_json(request), allowed=frozenset())
    return success(form_to_dict(_service().publish(_get_manageable(request, pk))))


@openapi_contract(
    path="/api/v1/forms/{pk}/close/",
    operations=(FORM_CLOSE_CONTRACT,),
)
@csrf_exempt
@require_auth
def form_close_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _validate_query(request, allowed=set())
    _body(read_json(request), allowed=frozenset())
    return success(form_to_dict(_service().close(_get_manageable(request, pk))))


@openapi_contract(
    path="/api/v1/forms/{pk}/submit/",
    operations=(FORM_SUBMIT_CONTRACT,),
)
@csrf_exempt
@require_auth
def form_submit_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")  # a responder submits
    _validate_query(request, allowed=set())
    form = _get_visible(request, pk)
    answers = _answers(_body(read_json(request), allowed=frozenset({"answers"})))
    principal = _request_principal(request)
    response = _service().submit(
        form,
        respondent=request.user,
        respondent_principal_kind=principal.kind,
        respondent_principal_id=principal.principal_id,
        answers=answers,
    )
    # Anonymous-safe: only echo the receipt, never the respondent.
    return created({"id": response.id, "created_at": response.created_at.isoformat()})


@openapi_contract(
    path="/api/v1/forms/{pk}/responses/",
    operations=FORM_RESPONSES_CONTRACTS,
)
@csrf_exempt
@require_auth
def form_responses_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _validate_query(request, allowed={"page", "page_size"})
    form = _get_manageable(request, pk)
    items, total, page, size = paginate(request, _service().responses_of(form))
    return paginated([response_to_dict(r) for r in items], total=total, page=page, page_size=size)


@openapi_contract(
    path="/api/v1/forms/{pk}/summary/",
    operations=FORM_SUMMARY_CONTRACTS,
)
@csrf_exempt
@require_auth
def form_summary_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _validate_query(request, allowed=set())
    return success(_service().summary(_get_manageable(request, pk)))


@openapi_contract(
    path="/api/v1/forms/{pk}/analyze/",
    operations=(FORM_ANALYZE_CONTRACT,),
)
@csrf_exempt
@require_auth
def form_analyze_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _validate_query(request, allowed=set())
    _body(read_json(request), allowed=frozenset())
    form = _get_manageable(request, pk)
    ai_request = _service().analyze(
        form,
        requested_by=request.user,
        requested_principal=_request_principal(request),
    )
    # 202 Accepted — the narrative is produced async; poll /ai/requests/{id}/.
    return success({"request_id": ai_request.pk, "status": ai_request.status}, status=202)


# --- helpers ---------------------------------------------------------------
def _create(request: HttpRequest, *, body: dict[str, Any]) -> HttpResponse:
    body = _body(body, allowed=_CREATE_FIELDS)
    title = str_field(body, "title", max_length=200).strip()
    if not title:
        raise ValidationException(
            "Title is required.", code="validation_error", fields={"title": ["This field is required."]}
        )
    dto = CreateFormDTO(
        title=title,
        description=str_field(body, "description", max_length=MAX_FORM_DESCRIPTION_CHARS),
        is_anonymous=bool_field(body, "is_anonymous"),
        allow_multiple=bool_field(body, "allow_multiple"),
        branch_id=int_field(body, "branch"),
        opens_at=_optional_datetime(body, "opens_at"),
        closes_at=_optional_datetime(body, "closes_at"),
        audience_roles=_audience_roles(body.get("audience_roles", [])),
        audience_user_ids=_audience_user_ids(body.get("audience_user_ids", [])),
    )
    scope = _scope(request)
    form = _service().create(
        dto,
        creator=request.user,
        creator_principal=scope.principal,
        is_unscoped=scope.write_unscoped,
        branch_ids=scope.write_branch_ids,
    )
    return created(form_to_dict(form))


def _update_changes(body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "title" in body or not partial:
        if "title" not in body:
            raise ValidationException(
                "Title is required.", code="validation_error", fields={"title": ["This field is required."]}
            )
        title = str_field(body, "title", max_length=200).strip()
        if not title:
            raise ValidationException(
                "Title may not be blank.", code="validation_error", fields={"title": ["May not be blank."]}
            )
        changes["title"] = title
    if "description" in body or not partial:
        if "description" in body and body["description"] is None:
            raise ValidationException(
                "description may not be null.",
                code="validation_error",
                fields={"description": ["May not be null."]},
            )
        changes["description"] = str_field(body, "description", max_length=MAX_FORM_DESCRIPTION_CHARS)
    if "is_anonymous" in body or not partial:
        changes["is_anonymous"] = bool_field(body, "is_anonymous")
    if "allow_multiple" in body or not partial:
        changes["allow_multiple"] = bool_field(body, "allow_multiple")
    if "opens_at" in body or not partial:
        changes["opens_at"] = _optional_datetime(body, "opens_at")
    if "closes_at" in body or not partial:
        changes["closes_at"] = _optional_datetime(body, "closes_at")
    if "audience_roles" in body or not partial:
        changes["audience_roles"] = _audience_roles(body.get("audience_roles", []))
    if "audience_user_ids" in body or not partial:
        changes["audience_user_ids"] = _audience_user_ids(body.get("audience_user_ids", []))
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
    if len(raw) > len(valid):
        raise ValidationException(
            "Too many audience roles.",
            code="validation_error",
            fields={"audience_roles": ["Remove duplicate or unnecessary role entries."]},
        )
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
    if len(raw) > 500:
        raise ValidationException(
            "Too many audience users.",
            code="validation_error",
            fields={"audience_user_ids": ["At most 500 users may be targeted."]},
        )
    out: list[int] = []
    for uid in raw:
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise ValidationException(
                "audience_user_ids must be integers.",
                code="validation_error",
                fields={"audience_user_ids": ["Each id must be an integer."]},
            )
        if uid not in out:
            out.append(uid)
    return out


def _field_dto(body: dict[str, Any]) -> AddFieldDTO:
    label = str_field(body, "label", max_length=255).strip()
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
    if len(options) > MAX_CHOICE_OPTIONS or any(
        not isinstance(option, str) or len(option.strip()) > MAX_CHOICE_OPTION_CHARS for option in options
    ):
        raise ValidationException(
            "Options are too large.",
            code="validation_error",
            fields={
                "options": [
                    f"Use at most {MAX_CHOICE_OPTIONS} choices of {MAX_CHOICE_OPTION_CHARS} characters."
                ]
            },
        )
    order = int_field(body, "order")
    if order is not None and order < 0:
        raise ValidationException(
            "order must be zero or greater.",
            code="validation_error",
            fields={"order": ["Must be zero or greater."]},
        )
    if field_type not in FormField.CHOICE_TYPES and options:
        raise ValidationException(
            "Only choice fields may define options.",
            code="validation_error",
            fields={"options": ["Options are only valid for choice fields."]},
        )
    return AddFieldDTO(
        label=label,
        field_type=field_type,
        required=bool_field(body, "required"),
        order=order,
        options=options,
        help_text=str_field(body, "help_text", max_length=255).strip(),
    )


def _answers(body: dict[str, Any]) -> list[dict]:
    raw = body.get("answers")
    if not isinstance(raw, list):
        raise ValidationException(
            "answers must be a list of {field, value} objects.",
            code="validation_error",
            fields={"answers": ["Must be a list of {field, value} objects."]},
        )
    if len(raw) > MAX_FORM_FIELDS:
        raise ValidationException(
            "Too many answers.",
            code="validation_error",
            fields={"answers": [f"At most {MAX_FORM_FIELDS} answers are allowed."]},
        )
    for item in raw:
        if not isinstance(item, dict) or "field" not in item:
            raise ValidationException(
                "each answer needs a 'field' id.",
                code="validation_error",
                fields={"answers": ["Each answer needs a 'field' id."]},
            )
        unknown = sorted(set(item) - {"field", "value"})
        if unknown:
            raise ValidationException(
                "An answer contains unknown fields.",
                code="validation_error",
                fields={name: ["Unknown field."] for name in unknown},
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


def _body(body: dict[str, Any], *, allowed: frozenset[str]) -> dict[str, Any]:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationException(
            "Request contains unknown fields.",
            code="validation_error",
            fields={field: ["Unknown field."] for field in unknown},
        )
    return body


def _validate_filters(request: HttpRequest) -> None:
    allowed = {"status", "branch", "is_anonymous", "search", "ordering", "page", "page_size"}
    _validate_query(request, allowed=allowed)
    status = request.GET.get("status")
    if "status" in request.GET and status not in Form.Status.values:
        raise ValidationException(
            "Invalid status filter.",
            code="validation_error",
            fields={"status": [f"Must be one of: {', '.join(Form.Status.values)}."]},
        )
    if "branch" in request.GET and positive_int_filter(request, "branch") is None:
        raise ValidationException(
            "Invalid branch filter.",
            code="validation_error",
            fields={"branch": ["Must be a positive integer."]},
        )
    if "is_anonymous" in request.GET:
        parse_bool(request.GET.get("is_anonymous"), "is_anonymous")
    ordering = request.GET.get("ordering")
    if "ordering" in request.GET and ordering not in {
        "created_at",
        "-created_at",
        "title",
        "-title",
    }:
        raise ValidationException(
            "Invalid ordering.",
            code="validation_error",
            fields={"ordering": ["Choose a declared ordering field."]},
        )
    search = request.GET.get("search")
    if search is not None and len(search) > 200:
        raise ValidationException(
            "Search term is too long.",
            code="validation_error",
            fields={"search": ["Must be at most 200 characters."]},
        )


def _validate_query(request: HttpRequest, *, allowed: set[str]) -> None:
    unknown = sorted(set(request.GET) - allowed)
    if unknown:
        raise ValidationException(
            "Unknown query parameter.",
            code="validation_error",
            fields={field: ["Unknown query parameter."] for field in unknown},
        )
    duplicates = sorted(name for name in request.GET if len(request.GET.getlist(name)) != 1)
    if duplicates:
        raise ValidationException(
            "Query parameter may be supplied only once.",
            code="validation_error",
            fields={field: ["Supply this parameter once."] for field in duplicates},
        )
    validate_pagination_filters(request)


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
