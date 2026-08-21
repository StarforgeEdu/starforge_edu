"""ORM-backed sales repository — branch-scoped reads for the seller's till."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.sales.interfaces.repositories import ISaleRepository
from apps.sales.models import Sale
from apps.students.models import StudentProfile
from core.repositories import BaseRepository

_SELECT_RELATED = ("student", "student__user", "branch", "payment_method", "sold_by", "refunded_by")


class SaleRepository(BaseRepository[Sale], ISaleRepository):
    model = Sale

    def _base(self) -> QuerySet[Sale]:
        return Sale.objects.select_related(*_SELECT_RELATED)

    def scoped(self, *, is_unscoped: bool, branch_ids: set[int]) -> QuerySet[Sale]:
        qs = self._base()
        if is_unscoped:
            return qs
        return qs.filter(branch_id__in=branch_ids)  # the seller's till only

    def get_scoped(self, *, is_unscoped: bool, branch_ids: set[int], pk: int) -> Sale | None:
        return self.scoped(is_unscoped=is_unscoped, branch_ids=branch_ids).filter(pk=pk).first()

    def get_student(self, *, student_id: int) -> StudentProfile | None:
        return StudentProfile.objects.select_related("branch").filter(pk=student_id).first()
