"""Parent endpoints — plain Django views over the layered architecture.

Scoping here is ROLE-based (TD-5), not branch-based: staff see every parent, a
parent sees only their own row. A detail read of an out-of-scope row therefore
404s (no existence leak), never 403. The two ``me/children`` routes are parent
self-service — authenticated-only, no parents:read grant, and they return only
the caller's own children.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.parents.dto.parent_dto import ParentCreateDTO
from apps.parents.interfaces.services import IParentService
from apps.parents.models import ParentProfile
from apps.parents.presenters import parent_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, no_content, paginated, success, validation_error

_RESOURCE = "parents"
_SEARCH = ("first_name", "last_name", "phone")
_ORDERING = ("created_at",)


def _service() -> IParentService:
    return container.resolve(IParentService)  # type: ignore[type-abstract]


def _students_payload(students) -> list:
    """Serialize a set of students to the shared read shape (no medical_notes) via
    the students app's layered presenter."""
    from apps.students.presenters import student_to_dict

    return [student_to_dict(s) for s in students]


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
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    parent = _service().get(user=request.user, roles=get_user_roles(request), pk=pk)
    if parent is None:
        raise NotFoundException(code="not_found")  # out-of-scope or absent -> 404, no leak

    if read:
        return success(parent_to_dict(parent))
    if request.method in ("PUT", "PATCH"):
        # PUT and PATCH are both partial (apply only the provided fields) — the
        # deliberate, mobile-friendly convention used across the off-DRF migration.
        updated = _service().update(parent, _changes(read_json(request)))
        return success(parent_to_dict(updated))
    if request.method == "DELETE":
        _service().delete(parent)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def parent_students_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    parent = _service().get(user=request.user, roles=get_user_roles(request), pk=pk)
    if parent is None:
        raise NotFoundException(code="not_found")
    return success(_students_payload(_service().students(parent)))


@csrf_exempt
@require_auth
def parent_credentials_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Issue a one-time parent password; the raw value is never stored or repeated."""
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    parent = _service().get(user=request.user, roles=get_user_roles(request), pk=pk)
    if parent is None:
        raise NotFoundException(code="not_found")
    from apps.users.services import issue_role_credentials

    return success(
        issue_role_credentials(
            parent,
            actor=request.user,
            resource_type="parents.ParentProfile",
        )
    )


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
    qs = _service().scoped_list(user=request.user, roles=get_user_roles(request))
    qs = apply_filters(
        request, qs, search_fields=_SEARCH, ordering_fields=_ORDERING, default_ordering="-created_at"
    )
    items, total, page, size = paginate(request, qs)
    return paginated([parent_to_dict(p) for p in items], total=total, page=page, page_size=size)


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
    phone = str_field(body, "phone")
    email = str_field(body, "email")
    if not phone and not email:
        return validation_error({"phone": ["Provide a phone or an email."]})
    dto = ParentCreateDTO(
        username=str_field(body, "username"),
        phone=phone,
        email=email,
        first_name=str_field(body, "first_name"),
        last_name=str_field(body, "last_name"),
        middle_name=str_field(body, "middle_name"),
        birthdate=_date_or_none(body, "birthdate"),
        gender=_choice(body, "gender", ParentProfile.Gender.values, allow_blank=True),
        workplace=str_field(body, "workplace"),
        notes=str_field(body, "notes"),
    )
    return created(parent_to_dict(_service().create(dto)))


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for field in ("first_name", "last_name", "middle_name", "phone", "email"):
        if field in body:
            changes[field] = str_field(body, field)
    if "birthdate" in body:
        changes["birthdate"] = _date_or_none(body, "birthdate")
    if "gender" in body:
        changes["gender"] = _choice(body, "gender", ParentProfile.Gender.values, allow_blank=True)
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active")
    if "workplace" in body:
        changes["workplace"] = str_field(body, "workplace")
    if "notes" in body:
        changes["notes"] = str_field(body, "notes")
    return changes
