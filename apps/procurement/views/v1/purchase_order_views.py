"""Procurement / purchase-order endpoints — plain Django views over the layered stack.

A PO is the itemised surface over the A-1 engine: raise a PO (line items totalling the
amount on an ApprovalRequest, kind="procurement"); approve + cashier-disburse happen in
the unified /approvals/ queue. Reads are role/ownership-scoped: a director/superuser and
any finance handler (approvals:approve / approvals:disburse) see every PO; a plain
requester only their own. Create is branch-scoped (a requester can't book spend against a
branch they don't belong to). No PUT/PATCH/DELETE (405).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.procurement.dto.purchase_order_dto import CreatePurchaseOrderDTO, PurchaseOrderLineDTO
from apps.procurement.interfaces.services import IPurchaseOrderService
from apps.procurement.presenters import po_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import decimal_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import Role, get_role_memberships, get_user_roles, has_permission_code
from core.responses import created, error, paginated, success

_RESOURCE = "procurement"
_MIN_QTY = Decimal("0.01")


def _service() -> IPurchaseOrderService:
    return container.resolve(IPurchaseOrderService)  # type: ignore[type-abstract]


def _is_unscoped(request: HttpRequest) -> bool:
    """A director/superuser and any finance handler (they approve/disburse the money) see
    every PO; everyone else is scoped to the POs they raised."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    if getattr(request.user, "is_superuser", False) or Role.DIRECTOR in roles:
        return True
    return has_permission_code(roles, "approvals:approve") or has_permission_code(roles, "approvals:disburse")


@csrf_exempt
@require_auth
def purchase_orders_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = _service().scoped_list(is_unscoped=_is_unscoped(request), user=request.user)
        qs = apply_filters(
            request,
            qs,
            filter_fields=("branch", "request__status"),
            ordering_fields=("created_at",),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([po_to_dict(p) for p in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create_po(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def purchase_order_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    po = _service().get_visible(is_unscoped=_is_unscoped(request), user=request.user, pk=pk)
    if po is None:
        raise NotFoundException(code="not_found")  # out-of-scope -> 404, no existence leak
    return success(po_to_dict(po))


# --- helpers ---------------------------------------------------------------
def _create_po(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    title = str_field(body, "title", max_length=200).strip()
    if not title:
        raise ValidationException(
            "title is required.", code="validation_error", fields={"title": ["This field is required."]}
        )
    supplier = str_field(body, "supplier", max_length=200).strip()
    if not supplier:
        raise ValidationException(
            "supplier is required.",
            code="validation_error",
            fields={"supplier": ["This field is required."]},
        )
    items = _items(body)
    branch = _resolve_branch(request, body)
    _assert_branch_in_scope(request, branch)

    dto = CreatePurchaseOrderDTO(
        title=title, supplier=supplier, description=str_field(body, "description"), items=items
    )
    po = _service().create(dto, requested_by=request.user, branch=branch)
    return created(po_to_dict(po))


def _items(body: dict[str, Any]) -> list[PurchaseOrderLineDTO]:
    """Validate the line-item list. Each element MUST be an object with a positive
    quantity and a non-negative price — the domain fn does ``item["quantity"]`` /
    ``Decimal(str(...))`` and would 500 (TypeError / InvalidOperation / KeyError) on a
    non-dict item or a non-numeric field; here it is a clean 400."""
    raw = body.get("items")
    if not isinstance(raw, list) or not raw:
        raise ValidationException(
            "A purchase order needs at least one line item.",
            code="validation_error",
            fields={"items": ["Provide a non-empty list of line items."]},
        )
    lines: list[PurchaseOrderLineDTO] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationException(
                f"Line item {index} must be an object.",
                code="validation_error",
                fields={"items": [f"Item {index} must be an object."]},
            )
        description = str_field(item, "description", max_length=255).strip()
        if not description:
            raise ValidationException(
                "Each line item needs a description.",
                code="validation_error",
                fields={"description": ["This field is required."]},
            )
        quantity = decimal_field(item, "quantity", max_digits=12)
        if quantity is None or quantity < _MIN_QTY:
            raise ValidationException(
                "Each line item quantity must be at least 0.01.",
                code="validation_error",
                fields={"quantity": ["Must be a number >= 0.01."]},
            )
        unit_price = decimal_field(item, "unit_price_uzs", max_digits=18)
        if unit_price is None or unit_price < 0:
            raise ValidationException(
                "A unit price cannot be negative.",
                code="validation_error",
                fields={"unit_price_uzs": ["Must be a number >= 0."]},
            )
        lines.append(
            PurchaseOrderLineDTO(description=description, quantity=quantity, unit_price_uzs=unit_price)
        )
    return lines


def _resolve_branch(request: HttpRequest, body: dict[str, Any]):
    """Resolve an OPTIONAL branch id to a non-archived Branch (unknown/archived -> 400);
    an absent/null branch is a centre-wide PO."""
    if body.get("branch") is None:
        return None
    branch_id = int_field(body, "branch", required=True)
    branch = _service().get_branch(branch_id=branch_id)  # type: ignore[arg-type]
    if branch is None:
        raise ValidationException(
            "Unknown or archived branch.",
            code="validation_error",
            fields={"branch": ["No such active branch."]},
        )
    return branch


def _assert_branch_in_scope(request: HttpRequest, branch) -> None:
    """A branch-scoped requester may only book spend against a branch they belong to;
    a director/superuser is unscoped, and a centre-wide (no branch) PO is allowed."""
    if branch is None:
        return
    req: Any = request
    roles = get_user_roles(req)
    if getattr(request.user, "is_superuser", False) or Role.DIRECTOR in roles:
        return
    my = {m.branch_id for m in get_role_memberships(req) if m.branch_id}
    if branch.id not in my:
        raise PermissionException(
            _("You can only raise a purchase order for your own branch."), code="branch_out_of_scope"
        )
