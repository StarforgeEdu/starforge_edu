"""ORM-backed loans repository — the annotated ApprovalRequest(kind="loan") queries."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import DecimalField, Q, QuerySet, Sum, Value
from django.db.models.functions import Coalesce

from apps.approvals.models import ApprovalRequest
from apps.approvals.services import KIND_LOAN
from apps.loans.interfaces.repositories import ILoanRepository
from apps.loans.models import LoanRepayment
from apps.org.models import Branch
from apps.users.models import User
from core.permissions import Role
from core.repositories import BaseRepository

# A staff loan goes to staff — never a student/parent (mirrors the old serializer).
_STAFF_ROLES = tuple(r for r in Role.ALL if r not in (Role.STUDENT, Role.PARENT))


class LoanRepository(BaseRepository[ApprovalRequest], ILoanRepository):
    model = ApprovalRequest

    def _base(self) -> QuerySet[ApprovalRequest]:
        return (
            ApprovalRequest.objects.filter(kind=KIND_LOAN)
            .select_related("branch", "requested_by", "decided_by", "disbursed_by", "ledger_entry")
            .annotate(
                repaid_uzs_annotated=Coalesce(
                    Sum("loan_repayments__amount_uzs"),
                    Value(Decimal("0")),
                    output_field=DecimalField(max_digits=18, decimal_places=2),
                )
            )
            .order_by("-created_at")  # annotate's GROUP BY can drop Meta.ordering
        )

    def scoped(
        self, *, is_unscoped: bool, is_collector: bool, user, branch_ids: set[int]
    ) -> QuerySet[ApprovalRequest]:
        qs = self._base()
        if is_unscoped:
            return qs
        if is_collector:
            # Finance handlers see their branches' loans (plus centre-wide ones).
            return qs.filter(Q(branch_id__in=branch_ids) | Q(branch__isnull=True))
        # A borrower sees only their own loans (raised by or for them).
        return qs.filter(Q(requested_by=user) | Q(payload__borrower_id=user.id))

    def get_scoped(
        self, *, is_unscoped: bool, is_collector: bool, user, branch_ids: set[int], pk: int
    ) -> ApprovalRequest | None:
        return (
            self.scoped(is_unscoped=is_unscoped, is_collector=is_collector, user=user, branch_ids=branch_ids)
            .filter(pk=pk)
            .first()
        )

    def annotated_get(self, *, pk: int) -> ApprovalRequest | None:
        return self._base().filter(pk=pk).first()

    def repayments_of(self, *, loan: ApprovalRequest) -> QuerySet[LoanRepayment]:
        return LoanRepayment.objects.filter(loan=loan).select_related(
            "payment_method", "branch", "recorded_by"
        )

    def get_branch(self, *, branch_id: int) -> Branch | None:
        return Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()

    def get_staff_borrower(self, *, user_id: int) -> User | None:
        return (
            User.objects.filter(
                pk=user_id,
                is_active=True,
                role_memberships__revoked_at__isnull=True,
                role_memberships__role__in=_STAFF_ROLES,
            )
            .distinct()
            .first()
        )
