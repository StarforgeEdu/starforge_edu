"""Sales endpoints — plain Django views over the layered architecture.

The till (sale:write) records a cash sale -> immutable money-IN ledger row; a refund
(sale:refund) writes a compensating money-OUT row. Sales rows are branch-scoped to the
seller's own till: a director/superuser sees every sale, any other role only their own
branches'. An out-of-scope detail/refund 404s (never a 403 existence leak), matching the
old ViewSet's get_object(). No PUT/PATCH/DELETE (405).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.sales.dto.sale_dto import RecordSaleDTO
from apps.sales.interfaces.services import ISaleService
from apps.sales.presenters import sale_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import decimal_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import Role, get_role_memberships, get_user_roles
from core.responses import created, error, paginated, success

_RESOURCE = "sale"
_MAX_QUANTITY = 1_000_000  # old serializer IntegerField(max_value=1_000_000)
_MIN_UNIT_PRICE = Decimal("0.01")  # old serializer DecimalField(min_value=0.01)


def _service() -> ISaleService:
    return container.resolve(ISaleService)  # type: ignore[type-abstract]


def _scope(request: HttpRequest) -> tuple[bool, set[int]]:
    """(is_unscoped, branch_ids): a director/superuser sees every till; anyone else only
    the branches of their (non-null-branch) role memberships."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    is_unscoped = bool(getattr(request.user, "is_superuser", False)) or Role.DIRECTOR in get_user_roles(req)
    branch_ids = {m.branch_id for m in get_role_memberships(req) if m.branch_id}
    return is_unscoped, branch_ids


@csrf_exempt
@require_auth
def sales_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        is_unscoped, branch_ids = _scope(request)
        qs = _service().scoped_list(is_unscoped=is_unscoped, branch_ids=branch_ids)
        qs = apply_filters(
            request,
            qs,
            filter_fields=("status", "branch", "student", "payment_method"),
            ordering_fields=("created_at", "amount_uzs"),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([sale_to_dict(s) for s in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create_sale(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def sale_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    is_unscoped, branch_ids = _scope(request)
    sale = _service().get_visible(is_unscoped=is_unscoped, branch_ids=branch_ids, pk=pk)
    if sale is None:
        raise NotFoundException(code="not_found")  # out-of-scope -> 404, no existence leak
    return success(sale_to_dict(sale))


@csrf_exempt
@require_auth
def sale_refund_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:refund")
    is_unscoped, branch_ids = _scope(request)
    sale = _service().get_visible(is_unscoped=is_unscoped, branch_ids=branch_ids, pk=pk)
    if sale is None:
        raise NotFoundException(code="not_found")  # cross-branch cashier -> 404 (not 422/403)
    body = read_json(request)
    result = _service().refund(sale, actor=request.user, reason=str_field(body, "reason", max_length=255))
    return success(sale_to_dict(result))


# --- helpers ---------------------------------------------------------------
def _create_sale(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    item = str_field(body, "item", max_length=200).strip()
    if not item:
        raise ValidationException(
            "item is required.", code="validation_error", fields={"item": ["This field is required."]}
        )
    quantity = int_field(body, "quantity", default=1)
    if quantity is None or quantity < 1 or quantity > _MAX_QUANTITY:
        raise ValidationException(
            "quantity must be between 1 and 1,000,000.",
            code="validation_error",
            fields={"quantity": [f"Must be an integer in [1, {_MAX_QUANTITY}]."]},
        )
    unit_price = decimal_field(body, "unit_price_uzs", max_digits=18)
    if unit_price is None or unit_price < _MIN_UNIT_PRICE:
        raise ValidationException(
            "unit_price_uzs must be at least 0.01.",
            code="validation_error",
            fields={"unit_price_uzs": ["Must be a number >= 0.01."]},
        )
    payment_method_id = int_field(body, "payment_method", required=True)
    student_id = int_field(body, "student", required=True)

    student = _service().get_student(student_id=student_id)  # type: ignore[arg-type]
    if student is None:
        raise ValidationException(
            "Unknown student.", code="validation_error", fields={"student": ["No such student."]}
        )
    is_unscoped, branch_ids = _scope(request)
    if not is_unscoped and student.branch_id not in branch_ids:
        raise PermissionException(
            _("You can only sell to a student in your own branch."), code="branch_out_of_scope"
        )

    dto = RecordSaleDTO(
        item=item,
        quantity=quantity,
        unit_price_uzs=unit_price,
        payment_method_id=payment_method_id,  # type: ignore[arg-type]  # required=True -> never None
        note=str_field(body, "note", max_length=255),
    )
    sale = _service().record(dto, student=student, sold_by=request.user)
    return created(sale_to_dict(sale))
