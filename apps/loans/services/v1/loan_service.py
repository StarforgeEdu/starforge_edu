"""Loans service — scoped reads + FK resolution, wrapping the preserved domain fns
(`request_loan` raises the A-1 request; `record_repayment` writes the money-IN ledger row)."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import QuerySet

from apps.approvals.models import ApprovalRequest
from apps.loans.dto.loan_dto import CreateLoanDTO
from apps.loans.interfaces.repositories import ILoanRepository
from apps.loans.interfaces.services import ILoanService
from apps.loans.models import LoanRepayment
from apps.loans.services import record_repayment, request_loan
from apps.org.models import Branch
from apps.users.models import User


class LoanService(ILoanService):
    def __init__(self, repository: ILoanRepository) -> None:
        self.repository = repository

    def scoped_list(
        self, *, is_unscoped: bool, is_collector: bool, user, branch_ids: set[int]
    ) -> QuerySet[ApprovalRequest]:
        return self.repository.scoped(
            is_unscoped=is_unscoped, is_collector=is_collector, user=user, branch_ids=branch_ids
        )

    def get_visible(
        self, *, is_unscoped: bool, is_collector: bool, user, branch_ids: set[int], pk: int
    ) -> ApprovalRequest | None:
        return self.repository.get_scoped(
            is_unscoped=is_unscoped, is_collector=is_collector, user=user, branch_ids=branch_ids, pk=pk
        )

    def annotated_get(self, *, pk: int) -> ApprovalRequest | None:
        return self.repository.annotated_get(pk=pk)

    def repayments_of(self, *, loan: ApprovalRequest) -> QuerySet[LoanRepayment]:
        return self.repository.repayments_of(loan=loan)

    def resolve_branch(self, *, branch_id: int) -> Branch | None:
        return self.repository.get_branch(branch_id=branch_id)

    def resolve_borrower(self, *, user_id: int) -> User | None:
        return self.repository.get_staff_borrower(user_id=user_id)

    def create(self, data: CreateLoanDTO, *, requested_by, branch, borrower) -> ApprovalRequest:
        return request_loan(
            requested_by=requested_by,
            amount_uzs=data.amount_uzs,
            title=data.title,
            description=data.description,
            branch=branch,
            borrower=borrower,
        )

    def repay(
        self, *, loan_id: int, amount_uzs: Decimal, payment_method_id: int, actor, note: str
    ) -> LoanRepayment:
        return record_repayment(
            loan_id=loan_id,
            amount_uzs=amount_uzs,
            payment_method_id=payment_method_id,
            actor=actor,
            note=note,
        )
