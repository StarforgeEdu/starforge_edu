"""Department endpoints — branch-scoped CRUD (object_scope='branch')."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.org.dto.org_dto import DepartmentCreateDTO
from apps.org.interfaces.services import IDepartmentService
from apps.org.presenters import department_to_dict
from apps.org.views.v1._shared import require_present, require_slug
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import (
    bool_field,
    decimal_field,
    int_field,
    read_json,
    reject_unknown_fields,
    trimmed_str_field,
)
from core.listing import apply_filters, paginate
from core.responses import created, error, no_content, paginated, success
from core.scoping import (
    assert_permission_membership_scope,
    request_permission_membership_allows,
    scope_to_permission_memberships,
)

_RESOURCE = "org"
_FILTERS = ("branch", "is_active")
_SEARCH = ("name", "slug")
_ORDERING = ("name", "created_at")
_CREATE_FIELDS = frozenset({"branch", "name", "slug", "description", "is_active", "head", "budget"})
# A department's branch is structural identity. Moving it in a generic PATCH
# invalidates memberships, cohorts, teachers, and immutable financial scope; a
# future dedicated workflow must reconcile all of those atomically.
_UPDATE_FIELDS = frozenset({"name", "slug", "description", "is_active", "head", "budget"})


def _service() -> IDepartmentService:
    return container.resolve(IDepartmentService)  # type: ignore[type-abstract]


def _query(request: HttpRequest, permission: str):
    return scope_to_permission_memberships(
        request,
        _service().list(),
        permission=permission,
        branch_field="branch_id",
        department_field="id",
    )


def _get(
    request: HttpRequest,
    pk: int,
    *,
    permission: str,
    for_update: bool = False,
):
    queryset = _query(request, permission)
    if for_update:
        # Repository eager-loading follows nullable ``head`` relations. Lock
        # only the department row: PostgreSQL rejects FOR UPDATE on the
        # nullable side of an outer join.
        queryset = queryset.select_for_update(of=("self",))
    department = queryset.filter(pk=pk).first()
    if department is None:
        raise NotFoundException(code="not_found")
    return department


def _payload(request: HttpRequest, department) -> dict[str, Any]:
    include_budget = request_permission_membership_allows(
        request,
        permission="finance:read",
        branch_id=department.branch_id,
        department_id=department.pk,
    )
    return department_to_dict(department, include_budget=include_budget)


@csrf_exempt
@require_auth
def departments_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        permission = f"{_RESOURCE}:read"
        check_perm(request, permission)
        qs = _query(request, permission)
        qs = apply_filters(
            request,
            qs,
            filter_fields=_FILTERS,
            search_fields=_SEARCH,
            ordering_fields=_ORDERING,
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([_payload(request, d) for d in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        permission = f"{_RESOURCE}:write"
        check_perm(request, permission)
        body = read_json(request)
        reject_unknown_fields(body, allowed=_CREATE_FIELDS)
        branch_id = int_field(body, "branch", required=True, min_value=1)
        # A department-scoped grant cannot create a sibling department. Only a
        # branch-wide (or organization-wide) org:write grant can mint this scope.
        assert_permission_membership_scope(
            request,
            permission=permission,
            branch_id=branch_id,
            department_id=None,
            enforce_department=True,
        )
        if "budget" in body:
            assert_permission_membership_scope(
                request,
                permission="finance:write",
                branch_id=branch_id,
                department_id=None,
                enforce_department=True,
            )
        name = trimmed_str_field(body, "name", required=True, max_length=200)
        slug = trimmed_str_field(body, "slug", required=True, max_length=100)
        require_present({"name": name, "slug": slug})
        require_slug("slug", slug)
        budget = decimal_field(body, "budget", max_digits=14)
        if budget is not None and budget < 0:
            raise ValidationException(
                "Budget cannot be negative.",
                code="validation_error",
                fields={"budget": ["Must be zero or greater."]},
            )
        dto = DepartmentCreateDTO(
            branch_id=branch_id,  # type: ignore[arg-type]
            name=name,
            slug=slug,
            description=trimmed_str_field(body, "description", max_length=4_000),
            is_active=bool_field(body, "is_active", default=True),
            head_id=int_field(body, "head", min_value=1),
            budget=budget,
        )
        return created(_payload(request, _service().create(dto)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def department_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    permission = f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write"
    check_perm(request, permission)
    if read:
        return success(_payload(request, _get(request, pk, permission=permission)))
    if request.method in ("PUT", "PATCH", "DELETE"):
        with transaction.atomic():
            department = _get(request, pk, permission=permission, for_update=True)
            if request.method in ("PUT", "PATCH"):
                body = read_json(request)
                reject_unknown_fields(body, allowed=_UPDATE_FIELDS)
                if "budget" in body:
                    assert_permission_membership_scope(
                        request,
                        permission="finance:write",
                        branch_id=department.branch_id,
                        department_id=department.pk,
                    )
                return success(_payload(request, _service().update(department, _changes(body))))
            _service().delete(department)
            return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "name" in body:
        changes["name"] = trimmed_str_field(body, "name", required=True, max_length=200)
        require_present({"name": changes["name"]})
    if "slug" in body:
        changes["slug"] = trimmed_str_field(body, "slug", required=True, max_length=100)
        require_present({"slug": changes["slug"]})
        require_slug("slug", changes["slug"])
    if "description" in body:
        changes["description"] = trimmed_str_field(body, "description", max_length=4_000)
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active", default=True)
    if "head" in body:
        changes["head"] = int_field(body, "head", min_value=1)
    if "budget" in body:
        budget = decimal_field(body, "budget", max_digits=14)
        if budget is not None and budget < 0:
            raise ValidationException(
                "Budget cannot be negative.",
                code="validation_error",
                fields={"budget": ["Must be zero or greater."]},
            )
        changes["budget"] = budget
    return changes
