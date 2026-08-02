"""Guardian (parent↔student link) endpoints — layered plain views.

Links are create + delete only (no PUT/PATCH — a change is delete-then-relink),
so the detail view answers 405 for PUT/PATCH. Reads and writes use the exact
permission-bearing parent/student scope.
"""

from __future__ import annotations

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.parents.dto.parent_dto import GuardianCreateDTO
from apps.parents.interfaces.services import IGuardianService
from apps.parents.models import Guardian
from apps.parents.presenters import guardian_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles, has_permission_code
from core.responses import created, error, no_content, paginated, success
from core.scoping import request_permission_membership_allows

_RESOURCE = "parents"
_FILTERS = ("parent", "student", "is_primary")


def _service() -> IGuardianService:
    return container.resolve(IGuardianService)  # type: ignore[type-abstract]


def _present(request: HttpRequest, guardian):
    student = guardian.student
    cohort = student.current_cohort if student.current_cohort_id is not None else None
    custody = request_permission_membership_allows(
        request,
        permission="safeguarding:read",
        branch_id=student.branch_id,
        department_id=cohort.department_id if cohort is not None else None,
        account_kinds={"staff"},
    )
    return guardian_to_dict(
        guardian,
        custody_notes=guardian.custody_notes if custody else None,
    )


def _present_collection(request: HttpRequest, guardians: list) -> list[dict]:
    """Decrypt custody notes in one query, only for exactly authorized rows."""
    ids = [guardian.pk for guardian in guardians]
    if not ids:
        return []
    roles = get_user_roles(request)
    if not getattr(request.user, "is_superuser", False) and not has_permission_code(
        roles,
        "safeguarding:read",
    ):
        return [guardian_to_dict(guardian) for guardian in guardians]
    authorized_ids = (
        _service()
        .scoped_list(
            user=request.user,
            roles=roles,
            permission="safeguarding:read",
        )
        .filter(pk__in=ids)
        .order_by()
        .values("pk")
    )
    # The broad directory queryset deliberately defers custody_notes. Django
    # keeps an explicit defer authoritative even after only(), which would turn
    # the comprehension into one decrypt query per row. Re-project authorized
    # ids through a fresh queryset so ciphertext is fetched/decrypted once.
    authorized = Guardian.objects.filter(pk__in=authorized_ids).only("pk", "custody_notes")
    notes_by_id = {guardian.pk: guardian.custody_notes for guardian in authorized}
    return [guardian_to_dict(guardian, custody_notes=notes_by_id.get(guardian.pk)) for guardian in guardians]


@csrf_exempt
@require_auth
def guardians_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = _service().scoped_list(
            user=request.user,
            roles=get_user_roles(request),
            permission=f"{_RESOURCE}:read",
        )
        qs = apply_filters(
            request, qs, filter_fields=_FILTERS, ordering_fields=("id",), default_ordering="id"
        )
        items, total, page, size = paginate(request, qs)
        return paginated(
            _present_collection(request, items),
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        _reject_unknown_fields(
            body,
            allowed=frozenset({"parent", "student", "relationship", "is_primary", "custody_notes"}),
        )
        custody_notes = str_field(body, "custody_notes")
        if custody_notes:
            check_perm(request, "safeguarding:write")
        dto = GuardianCreateDTO(
            parent_id=int_field(body, "parent", required=True),  # type: ignore[arg-type]
            student_id=int_field(body, "student", required=True),  # type: ignore[arg-type]
            relationship=str_field(body, "relationship", max_length=16),
            is_primary=bool_field(body, "is_primary"),
            custody_notes=custody_notes,
        )
        guardian = _service().create(dto, user=request.user, roles=get_user_roles(request))
        return created(_present(request, guardian))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def guardian_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    if not read:
        return _guardian_detail_write(request, pk)
    check_perm(request, f"{_RESOURCE}:read")
    guardian = _service().get(
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:read",
        pk=pk,
    )
    if guardian is None:
        raise NotFoundException(code="not_found")
    return success(_present(request, guardian))


@transaction.atomic
def _guardian_detail_write(request: HttpRequest, pk: int) -> HttpResponse:
    check_perm(request, f"{_RESOURCE}:write")
    guardian = _service().get(
        user=request.user,
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:write",
        pk=pk,
    )
    if guardian is None:
        raise NotFoundException(code="not_found")
    if request.method == "DELETE":
        _reject_unknown_fields(read_json(request), allowed=frozenset())
        _service().revoke(
            guardian,
            user=request.user,
            roles=get_user_roles(request),
            actor=request.user,
        )
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _reject_unknown_fields(body: dict, *, allowed: frozenset[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        from django.utils.translation import gettext_lazy as _

        from core.exceptions import ValidationException

        raise ValidationException(
            _("Unsupported guardian field."),
            code="validation_error",
            fields={field: [_("This field is not supported.")] for field in unknown},
        )
