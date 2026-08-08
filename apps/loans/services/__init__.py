"""Staff-loan services (F21-1): raise a loan request on the A-1 engine, and record
repayments against a disbursed loan (money IN -> immutable LedgerEntry).

Preserved verbatim; the layered service (services/v1/loan_service.py) wraps these and
the presenter imports `repaid_total`. `request_loan` / `record_repayment` are the write
paths; `repaid_total` / `outstanding_for` compute the balance.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _

from apps.approvals.models import ApprovalRequest, LedgerEntry
from apps.approvals.services import KIND_LOAN, create_request
from apps.loans.models import LoanRepayment
from core.exceptions import ConflictException, NotFoundException, UnprocessableEntity
from core.idempotency import (
    assert_principal_actor,
    lock_idempotency_key,
    operation_fingerprint,
    principal_scoped_key_hash,
    validate_idempotency_key,
)
from core.role_principals import STAFF_PRINCIPAL_KINDS, RolePrincipal


def request_loan(
    *,
    requested_by,
    amount_uzs: Decimal,
    title: str,
    description: str = "",
    branch=None,
    borrower=None,
    allowed_branch_ids: set[int] | None = None,
):
    """Raise a loan request on the A-1 engine. The borrower defaults to the
    requester (staff borrowing for themselves); a manager may name another staff
    member. Approval + disbursement happen through the unified approvals queue."""
    borrower_id = getattr(borrower, "id", None) or getattr(requested_by, "id", None)
    return create_request(
        kind=KIND_LOAN,
        title=title,
        requested_by=requested_by,
        amount_uzs=amount_uzs,
        description=description,
        branch=branch,
        payload={"borrower_id": borrower_id},
        allowed_branch_ids=allowed_branch_ids,
    )


def repaid_total(loan: ApprovalRequest) -> Decimal:
    agg = LoanRepayment.objects.filter(loan=loan).aggregate(total=Sum("amount_uzs"))
    return agg["total"] or Decimal("0")


def outstanding_for(loan: ApprovalRequest) -> Decimal | None:
    """What is still owed on a DISBURSED loan; None before the money has gone out
    (there is nothing to repay until then)."""
    if loan.status != ApprovalRequest.Status.DISBURSED or loan.amount_uzs is None:
        return None
    return loan.amount_uzs - repaid_total(loan)


def _locked_loan(loan_id: int) -> ApprovalRequest:
    loan = ApprovalRequest.objects.select_for_update().filter(pk=loan_id, kind=KIND_LOAN).first()
    if loan is None:
        raise NotFoundException(_("Loan not found."), code="loan_not_found")
    return loan


@transaction.atomic
def record_repayment(
    *,
    loan_id: int,
    amount_uzs: Decimal,
    payment_method_id: int,
    actor,
    principal: RolePrincipal,
    idempotency_key: str,
    is_unscoped: bool,
    branch_ids: set[int],
    note: str = "",
) -> LoanRepayment:
    """Record a repayment against a disbursed loan: writes one immutable money-IN
    LedgerEntry and links it. The loan row is locked, so two concurrent repayments
    serialize and can never together exceed the outstanding balance."""
    from apps.finance.models import PaymentMethod

    assert_principal_actor(
        actor=actor,
        principal=principal,
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
    )
    amount = amount_uzs.quantize(Decimal("0.01"))
    clean_note = (note or "")[:255]
    raw_key = validate_idempotency_key(idempotency_key)
    key_hash = principal_scoped_key_hash(namespace="loan-repayment", principal=principal, raw=raw_key)
    fingerprint = operation_fingerprint(
        namespace="loan-repayment",
        action="repay",
        resource={"loan_id": loan_id},
        body={
            "amount_uzs": str(amount),
            "note": clean_note,
            "payment_method_id": payment_method_id,
        },
    )
    lock_idempotency_key(namespace="loan-repayment", principal=principal, key_hash=key_hash)
    loan = _locked_loan(loan_id)
    # Match the collector repository's current visibility against the locked row.
    # Null-branch legacy money stays quarantined from scoped handlers until its
    # historical ownership has been reviewed and backfilled.
    if not is_unscoped and loan.branch_id not in branch_ids:
        raise NotFoundException(code="not_found")

    existing = LoanRepayment.objects.filter(idempotency_key_hash=key_hash).first()
    if existing is not None:
        if (
            existing.recorded_by_principal_kind != principal.kind
            or existing.recorded_by_principal_id != principal.principal_id
            or existing.operation_fingerprint != fingerprint
        ):
            raise ConflictException(
                _("This idempotency key belongs to a different loan repayment."),
                code="idempotency_key_reused",
            )
        # Current collection scope was rechecked against the locked loan above. Return
        # before mutable loan/provider validation so a settled loan or retired payment
        # method cannot make a successful response-loss retry fail.
        return existing

    if loan.status != ApprovalRequest.Status.DISBURSED:
        raise UnprocessableEntity(_("Only a disbursed loan can be repaid."), code="loan_not_disbursed")
    if loan.amount_uzs is None:  # a disbursed loan always carries its amount; defensive
        raise UnprocessableEntity(_("This loan has no amount to repay."), code="loan_no_amount")
    # Computed under the loan lock, so a competing repayment can't have slipped in.
    outstanding = loan.amount_uzs - repaid_total(loan)
    if outstanding <= 0:
        raise UnprocessableEntity(_("This loan is already settled."), code="loan_already_settled")
    if amount > outstanding:
        raise UnprocessableEntity(
            _("A repayment cannot exceed the outstanding balance."), code="loan_repayment_exceeds"
        )

    method = PaymentMethod.objects.filter(pk=payment_method_id, is_active=True).first()
    if method is None:
        raise UnprocessableEntity(_("Unknown or inactive payment method."), code="payment_method_invalid")

    entry = LedgerEntry.objects.create(
        direction=LedgerEntry.Direction.IN,
        entry_type="loan_repayment",
        amount_uzs=amount,
        branch=loan.branch,
        # Name the borrower (stamped into the payload at request time), so the IN
        # rows reconcile against the OUT disbursement for the same person.
        # Truncated to the column width (varchar(200)) to never 500 on a long name.
        party_label=(
            loan.payload.get("party_label")
            or (loan.requested_by.get_full_name() if loan.requested_by else "")
        )[:200],
        payment_method=method,
        source_kind="approval_request",
        source_id=loan.pk,
        note=(clean_note or loan.title)[:255],
        created_by=actor,
    )
    repaid_after = loan.amount_uzs - outstanding + amount
    outstanding_after = outstanding - amount
    from apps.loans.presenters import loan_to_dict

    response_snapshot = loan_to_dict(
        loan,
        repaid_uzs=repaid_after,
        outstanding_uzs=outstanding_after,
    )
    return LoanRepayment.objects.create(
        loan=loan,
        amount_uzs=amount,
        branch=loan.branch,
        payment_method=method,
        ledger_entry=entry,
        recorded_by=actor,
        recorded_by_principal_kind=principal.kind,
        recorded_by_principal_id=principal.principal_id,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
        response_snapshot=response_snapshot,
        repaid_after_uzs=repaid_after,
        outstanding_after_uzs=outstanding_after,
        note=clean_note,
    )
