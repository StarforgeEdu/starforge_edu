"""Guardian (parent↔student link) endpoints — layered plain views.

Links are create + delete only (no PUT/PATCH — a change is delete-then-relink),
so the detail view answers 405 for PUT/PATCH. Same role scoping as parents.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.parents.dto.parent_dto import GuardianCreateDTO
from apps.parents.interfaces.services import IGuardianService
from apps.parents.presenters import guardian_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, no_content, paginated, success

_RESOURCE = "parents"
_FILTERS = ("parent", "student", "is_primary")


def _service() -> IGuardianService:
    return container.resolve(IGuardianService)  # type: ignore[type-abstract]


@csrf_exempt
@require_auth
def guardians_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = _service().scoped_list(user=request.user, roles=get_user_roles(request))
        qs = apply_filters(request, qs, filter_fields=_FILTERS, ordering_fields=("id",), default_ordering="id")
        items, total, page, size = paginate(request, qs)
        return paginated([guardian_to_dict(g) for g in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        dto = GuardianCreateDTO(
            parent_id=int_field(body, "parent", required=True),  # type: ignore[arg-type]
            student_id=int_field(body, "student", required=True),  # type: ignore[arg-type]
            relationship=str_field(body, "relationship"),
            is_primary=bool_field(body, "is_primary"),
            custody_notes=str_field(body, "custody_notes"),
        )
        return created(guardian_to_dict(_service().create(dto)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def guardian_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    guardian = _service().get(user=request.user, roles=get_user_roles(request), pk=pk)
    if guardian is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(guardian_to_dict(guardian))
    if request.method == "DELETE":
        _service().delete(guardian)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)
