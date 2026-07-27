"""Sales-domain service port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.sales.dto.sale_dto import RecordSaleDTO
from apps.sales.models import Sale
from apps.students.models import StudentProfile


class ISaleService(ABC):
    @abstractmethod
    def scoped_list(self, *, is_unscoped: bool, branch_ids: set[int]) -> QuerySet[Sale]: ...

    @abstractmethod
    def get_visible(self, *, is_unscoped: bool, branch_ids: set[int], pk: int) -> Sale | None: ...

    @abstractmethod
    def get_student(self, *, student_id: int) -> StudentProfile | None: ...

    @abstractmethod
    def record(self, data: RecordSaleDTO, *, student, sold_by) -> Sale: ...

    @abstractmethod
    def refund(self, sale: Sale, *, actor, reason: str) -> Sale: ...
