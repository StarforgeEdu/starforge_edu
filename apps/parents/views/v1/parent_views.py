"""Parent endpoints — plain Django views over the layered architecture.

Staff reads and writes use the exact branch/department membership granting the
requested permission. A detail read outside that scope returns 404 (no
existence leak), never 403. The two ``me/children`` routes are parent
self-service — authenticated-only, no parents:read grant, and return only the
caller's own children.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.parents.dto.parent_dto import ParentCreateDTO
from apps.parents.interfaces.services import IParentService
from apps.parents.models import ParentProfile
from apps.parents.presenters import parent_list_to_dict, parent_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, no_content, paginated, success, validation_error

_RESOURCE = "parents"
_SEARCH = ("first_name", "last_name", "phone")
_ORDERING = ("created_at",)
_CREATE_FIELDS = frozenset(
    {
        "username",
        "phone",
        "email",
        "first_name",
        "last_name",
        "middle_name",
        "birthdate",
        "gender",
        "workplace",
        "notes",
        "branch",
        "department",
    }
)
_UPDATE_FIELDS = frozenset(
    {
        "phone",
        "email",
        "first_name",
        "last_name",
        "middle_name",
        "birthdate",
        "gender",
        "workplace",
        "notes",
    }
)


def _service() -> IParentService:
    return container.resolve(IParentService)  # type: ignore[type-abstract]


def _students_payload(students) -> list:
    """Serialize a set of students to the shared read shape (no medical_notes) via
    the students app's layered presenter."""
    from apps.students.presenters import student_to_dict

    return [student_to_dict(s) for s in students]


def _present_parent(request: HttpRequest, parent: ParentProfile) -> dict[str, Any]:
    notes = _service().scope_allows(
        parent,
        user=request.user,
        roles=get_user_roles(request),
        permission="safeguarding:read",
    )
    return parent_to_dict(parent, notes=notes)


@csrf_exempt
@require_auth
def parents_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        return _list(request)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def parent_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    if not read:
        return _parent_detail_write(request, pk)
    check_perm(request, f"{_RESOURCE}:read")
    parent = _service().get(
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:read",
        pk=pk,
    )
    if parent is None:
        raise NotFoundException(code="not_found")  # out-of-scope or absent -> 404, no leak
    return success(_present_parent(request, parent))


@transaction.atomic
def _parent_detail_write(request: HttpRequest, pk: int) -> HttpResponse:
    check_perm(request, f"{_RESOURCE}:write")
    parent = _service().get(
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:write",
        pk=pk,
    )
    if parent is None:
        raise NotFoundException(code="not_found")
    _service().assert_manage_scope(
        parent,
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:write",
    )
    if request.method in ("PUT", "PATCH"):
        # PUT and PATCH are both partial (apply only the provided fields) — the
        # deliberate, mobile-friendly convention used across the off-DRF migration.
        body = read_json(request)
        _reject_unknown_fields(body, allowed=_UPDATE_FIELDS, operation=_("parent update"))
        changes = _changes(body)
        if "notes" in changes:
            check_perm(request, "safeguarding:write")
            _service().assert_manage_scope(
                parent,
                user=request.user,
                roles=get_user_roles(request),
                permission="safeguarding:write",
            )
        updated = _service().update(parent, changes)
        return success(_present_parent(request, updated))
    if request.method == "DELETE":
        _reject_unknown_fields(read_json(request), allowed=frozenset(), operation=_("parent deactivation"))
        _service().deactivate(parent, actor=request.user)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def parent_students_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    parent = _service().get(
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:read",
        pk=pk,
    )
    if parent is None:
        raise NotFoundException(code="not_found")
    return success(
        _students_payload(
            _service().students(
                parent,
                user=request.user,
                roles=get_user_roles(request),
                permission=f"{_RESOURCE}:read",
            )
        )
    )


@csrf_exempt
@require_auth
@transaction.atomic
def parent_credentials_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Issue a one-time parent password; the raw value is never stored or repeated."""
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    parent = _service().get(
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:write",
        pk=pk,
    )
    if parent is None:
        raise NotFoundException(code="not_found")
    _service().assert_manage_scope(
        parent,
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:write",
    )
    _reject_unknown_fields(read_json(request), allowed=frozenset(), operation=_("parent credentials"))
    return success(_service().issue_credentials(parent, actor=request.user))


# --- parent self-service (no parents:read grant; own rows only) ------------
@csrf_exempt
@require_auth
def parent_children_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    parent = _service().require_profile(request.user)
    return success(_students_payload(_service().students(parent)))


@csrf_exempt
@require_auth
def parent_child_report_view(request: HttpRequest, student_id: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    from apps.students.selectors import student_report

    parent = _service().require_profile(request.user)
    student = _service().child_or_404(parent, student_id)
    return success(student_report(student=student))


# --- helpers ---------------------------------------------------------------
def _list(request: HttpRequest) -> HttpResponse:
    qs = _service().scoped_list(
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:read",
    )
    qs = apply_filters(
        request, qs, search_fields=_SEARCH, ordering_fields=_ORDERING, default_ordering="-created_at"
    )
    items, total, page, size = paginate(request, qs)
    return paginated([parent_list_to_dict(p) for p in items], total=total, page=page, page_size=size)


def _date_or_none(data: dict[str, Any], name: str):
    """Parse an optional YYYY-MM-DD date: None when absent/blank; 400 on a bad value."""
    raw = data.get(name)
    if raw in (None, ""):
        return None
    parsed = None
    if isinstance(raw, str):
        from django.utils.dateparse import parse_date

        try:
            parsed = parse_date(raw)
        except ValueError:
            parsed = None
    if parsed is None:
        raise ValidationException(
            f"Invalid {name}.",
            code="validation_error",
            fields={name: ["Enter a valid date (YYYY-MM-DD)."]},
        )
    return parsed


def _choice(data: dict[str, Any], name: str, choices, *, allow_blank: bool = False, default: str = "") -> str:
    raw = data.get(name)
    if raw in (None, ""):
        return "" if allow_blank and raw == "" else default
    value = str(raw)
    if value not in choices:
        raise ValidationException(
            f"Invalid {name}.", code="validation_error", fields={name: ["Not a valid choice."]}
        )
    return value


def _create(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    _reject_unknown_fields(body, allowed=_CREATE_FIELDS, operation=_("parent creation"))
    phone = str_field(body, "phone", max_length=32)
    email = str_field(body, "email", max_length=254)
    if not phone and not email:
        return validation_error({"phone": ["Provide a phone or an email."]})
    notes = str_field(body, "notes")
    if notes:
        check_perm(request, "safeguarding:write")
    dto = ParentCreateDTO(
        username=str_field(body, "username", max_length=150),
        phone=phone,
        email=email,
        first_name=str_field(body, "first_name", max_length=150),
        last_name=str_field(body, "last_name", max_length=150),
        middle_name=str_field(body, "middle_name", max_length=150),
        birthdate=_date_or_none(body, "birthdate"),
        gender=_choice(body, "gender", ParentProfile.Gender.values, allow_blank=True),
        workplace=str_field(body, "workplace", max_length=200),
        notes=notes,
        branch_id=int_field(body, "branch"),
        department_id=int_field(body, "department"),
    )
    parent = _service().create(
        dto,
        user=request.user,
        roles=get_user_roles(request),
    )
    return created(_present_parent(request, parent))


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    identity_lengths = {
        "first_name": 150,
        "last_name": 150,
        "middle_name": 150,
        "phone": 32,
        "email": 254,
    }
    for field, max_length in identity_lengths.items():
        if field in body:
            changes[field] = str_field(body, field, max_length=max_length)
    if "birthdate" in body:
        changes["birthdate"] = _date_or_none(body, "birthdate")
    if "gender" in body:
        changes["gender"] = _choice(body, "gender", ParentProfile.Gender.values, allow_blank=True)
    if "workplace" in body:
        changes["workplace"] = str_field(body, "workplace", max_length=200)
    if "notes" in body:
        changes["notes"] = str_field(body, "notes")
    return changes


def _reject_unknown_fields(
    body: dict[str, Any],
    *,
    allowed: frozenset[str],
    operation,
) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationException(
            _("Unsupported field in %(operation)s.") % {"operation": operation},
            code="validation_error",
            fields={field: [_("This field is not supported.")] for field in unknown},
        )
