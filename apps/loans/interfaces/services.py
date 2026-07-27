"""Loans-domain service port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from django.db.models import QuerySet

from apps.approvals.models import ApprovalRequest
from apps.loans.dto.loan_dto import CreateLoanDTO
from apps.loans.models import LoanRepayment
from apps.org.models import Branch
from apps.users.models import User


class ILoanService(ABC):
    @abstractmethod
    def scoped_list(
        self, *, is_unscoped: bool, is_collector: bool, user, branch_ids: set[int]
    ) -> QuerySet[ApprovalRequest]: ...

    @abstractmethod
    def get_visible(
        self, *, is_unscoped: bool, is_collector: bool, user, branch_ids: set[int], pk: int
    ) -> ApprovalRequest | None: ...

    @abstractmethod
    def annotated_get(self, *, pk: int) -> ApprovalRequest | None: ...

    @abstractmethod
    def repayments_of(self, *, loan: ApprovalRequest) -> QuerySet[LoanRepayment]: ...

    @abstractmethod
    def resolve_branch(self, *, branch_id: int) -> Branch | None: ...

    @abstractmethod
    def resolve_borrower(self, *, user_id: int) -> User | None: ...

    @abstractmethod
    def create(self, data: CreateLoanDTO, *, requested_by, branch, borrower) -> ApprovalRequest: ...

    @abstractmethod
    def repay(
        self, *, loan_id: int, amount_uzs: Decimal, payment_method_id: int, actor, note: str
    ) -> LoanRepayment: ...
