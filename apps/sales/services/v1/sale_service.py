"""Sales service — thin orchestration over the preserved domain functions
(`record_sale` / `refund_sale`) plus branch-scoped reads and student resolution."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.sales.dto.sale_dto import RecordSaleDTO
from apps.sales.interfaces.repositories import ISaleRepository
from apps.sales.interfaces.services import ISaleService
from apps.sales.models import Sale
from apps.sales.services import record_sale, refund_sale
from apps.students.models import StudentProfile


class SaleService(ISaleService):
    def __init__(self, repository: ISaleRepository) -> None:
        self.repository = repository

    def scoped_list(self, *, is_unscoped: bool, branch_ids: set[int]) -> QuerySet[Sale]:
        return self.repository.scoped(is_unscoped=is_unscoped, branch_ids=branch_ids)

    def get_visible(self, *, is_unscoped: bool, branch_ids: set[int], pk: int) -> Sale | None:
        return self.repository.get_scoped(is_unscoped=is_unscoped, branch_ids=branch_ids, pk=pk)

    def get_student(self, *, student_id: int) -> StudentProfile | None:
        return self.repository.get_student(student_id=student_id)

    def record(self, data: RecordSaleDTO, *, student, sold_by) -> Sale:
        return record_sale(
            item=data.item,
            quantity=data.quantity,
            unit_price_uzs=data.unit_price_uzs,
            student=student,
            payment_method_id=data.payment_method_id,
            sold_by=sold_by,
            note=data.note,
        )

    def refund(self, sale: Sale, *, actor, reason: str) -> Sale:
        return refund_sale(sale_id=sale.pk, actor=actor, reason=reason)
