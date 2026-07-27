"""Staff-loan endpoints — plain Django views over the layered stack.

A loan is the loan-specific surface over the A-1 engine: raise a loan (kind="loan"
ApprovalRequest), see outstanding balances, and record repayments (money IN). The
approve/disburse decision lives in the unified /approvals/ queue. Reads mirror that
queue's scoping: a director/superuser and any finance handler (loan:collect) see the
relevant loans; a borrower only their own. No PUT/PATCH/DELETE (405).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.loans.dto.loan_dto import CreateLoanDTO
from apps.loans.interfaces.services import ILoanService
from apps.loans.presenters import loan_to_dict, repayment_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import decimal_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import Role, get_role_memberships, get_user_roles, has_permission_code
from core.responses import created, error, paginated, success
from core.scoping import assert_branch_id_in_scope

_RESOURCE = "loan"
_MIN_AMOUNT = Decimal("0.01")


def _service() -> ILoanService:
    return container.resolve(ILoanService)  # type: ignore[type-abstract]


def _scope(request: HttpRequest) -> tuple[bool, bool, set[int]]:
    """(is_unscoped, is_collector, branch_ids). is_unscoped = director/superuser (all);
    is_collector = holds loan:collect (their branches + centre-wide); else borrower-scoped."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    is_unscoped = bool(getattr(request.user, "is_superuser", False)) or Role.DIRECTOR in roles
    is_collector = has_permission_code(roles, "loan:collect")
    branch_ids = {m.branch_id for m in get_role_memberships(req) if m.branch_id}
    return is_unscoped, is_collector, branch_ids


def _get_visible(request: HttpRequest, pk: int):
    is_unscoped, is_collector, branch_ids = _scope(request)
    loan = _service().get_visible(
        is_unscoped=is_unscoped, is_collector=is_collector, user=request.user, branch_ids=branch_ids, pk=pk
    )
    if loan is None:
        raise NotFoundException(code="not_found")  # out-of-scope -> 404, no existence leak
    return loan


@csrf_exempt
@require_auth
def loans_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):  # HEAD -> list (200), as the old ViewSet mapped it
        check_perm(request, f"{_RESOURCE}:read")
        is_unscoped, is_collector, branch_ids = _scope(request)
        qs = _service().scoped_list(
            is_unscoped=is_unscoped, is_collector=is_collector, user=request.user, branch_ids=branch_ids
        )
        qs = apply_filters(
            request,
            qs,
            filter_fields=("status", "branch"),
            ordering_fields=("created_at", "amount_uzs"),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([loan_to_dict(loan) for loan in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create_loan(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def loan_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    return success(loan_to_dict(_get_visible(request, pk)))


@csrf_exempt
@require_auth
def loan_repay_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:collect")
    loan = _get_visible(request, pk)  # scoped -> a cross-branch collector 404s
    body = read_json(request)
    amount = decimal_field(body, "amount_uzs", max_digits=18)
    if amount is None or amount < _MIN_AMOUNT:
        raise ValidationException(
            "amount_uzs must be at least 0.01.",
            code="validation_error",
            fields={"amount_uzs": ["Must be a number >= 0.01."]},
        )
    payment_method_id = int_field(body, "payment_method", required=True)
    _service().repay(
        loan_id=loan.pk,
        amount_uzs=amount,
        payment_method_id=payment_method_id,  # type: ignore[arg-type]  # required=True -> never None
        actor=request.user,
        note=str_field(body, "note", max_length=255),
    )
    fetched = _service().annotated_get(pk=loan.pk) or loan
    return created(loan_to_dict(fetched))


@csrf_exempt
@require_auth
def loan_repayments_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    loan = _get_visible(request, pk)
    rows = _service().repayments_of(loan=loan)
    return success([repayment_to_dict(r) for r in rows])


# --- helpers ---------------------------------------------------------------
def _create_loan(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    title = str_field(body, "title", max_length=200).strip()
    if not title:
        raise ValidationException(
            "title is required.", code="validation_error", fields={"title": ["This field is required."]}
        )
    amount = decimal_field(body, "amount_uzs", max_digits=18)
    if amount is None or amount < _MIN_AMOUNT:
        raise ValidationException(
            "amount_uzs must be at least 0.01.",
            code="validation_error",
            fields={"amount_uzs": ["Must be a number >= 0.01."]},
        )
    branch = _resolve_branch(request, body)
    borrower = _resolve_borrower(request, body)
    dto = CreateLoanDTO(title=title, amount_uzs=amount, description=str_field(body, "description"))
    loan = _service().create(dto, requested_by=request.user, branch=branch, borrower=borrower)
    fetched = _service().annotated_get(pk=loan.pk) or loan
    return created(loan_to_dict(fetched))


def _resolve_branch(request: HttpRequest, body: dict[str, Any]):
    """Resolve an OPTIONAL branch id to a non-archived Branch (unknown/archived -> 400)."""
    if body.get("branch") is None:
        return None
    branch_id = int_field(body, "branch", required=True)
    branch = _service().resolve_branch(branch_id=branch_id)  # type: ignore[arg-type]
    if branch is None:
        raise ValidationException(
            "Unknown or archived branch.",
            code="validation_error",
            fields={"branch": ["No such active branch."]},
        )
    # A loans:write holder may only attribute a loan (and its OUT disbursement +
    # repayments) to their OWN branch — every read path here is branch-scoped, so
    # accepting an arbitrary branch on the write path mis-attributes the money and
    # leaks a branch-id existence oracle. No-op for an unscoped caller (director).
    assert_branch_id_in_scope(request, branch.id)
    return branch


def _resolve_borrower(request: HttpRequest, body: dict[str, Any]):
    """Resolve an OPTIONAL borrower to an active STAFF user (a staff loan can't name a
    student/parent or an unknown/inactive user -> 400); default is the requester."""
    if body.get("borrower") is None:
        return None
    borrower_id = int_field(body, "borrower", required=True)
    borrower = _service().resolve_borrower(user_id=borrower_id)  # type: ignore[arg-type]
    if borrower is None:
        raise ValidationException(
            "The borrower must be an active staff member.",
            code="validation_error",
            fields={"borrower": ["No such active staff member."]},
        )
    return borrower
