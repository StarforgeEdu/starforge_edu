"""Loans-domain repository port.

A "loan" is an approvals.ApprovalRequest (kind="loan"), annotated with its repaid total.
Visibility mirrors the approvals queue: a director/superuser sees every loan; a finance
handler (loan:collect) sees their branches' loans plus centre-wide ones; a borrower sees
only loans they raised or that name them (payload borrower_id). Repayments are a separate
LoanRepayment model.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.approvals.models import ApprovalRequest
from apps.loans.models import LoanRepayment
from apps.org.models import Branch
from apps.users.models import User
from core.interfaces import IBaseRepository


class ILoanRepository(IBaseRepository[ApprovalRequest]):
    def scoped(
        self, *, is_unscoped: bool, is_collector: bool, user, branch_ids: set[int]
    ) -> QuerySet[ApprovalRequest]:
        raise NotImplementedError

    def get_scoped(
        self, *, is_unscoped: bool, is_collector: bool, user, branch_ids: set[int], pk: int
    ) -> ApprovalRequest | None:
        raise NotImplementedError

    def annotated_get(self, *, pk: int) -> ApprovalRequest | None:
        raise NotImplementedError

    def repayments_of(self, *, loan: ApprovalRequest) -> QuerySet[LoanRepayment]:
        raise NotImplementedError

    def get_branch(self, *, branch_id: int) -> Branch | None:
        raise NotImplementedError

    def get_staff_borrower(self, *, user_id: int) -> User | None:
        raise NotImplementedError
