"""Repository ports for the finance app."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.finance.models import (
    CashierShift,
    Discount,
    Expense,
    FeeSchedule,
    Invoice,
    PaymentMethod,
    StatementExport,
)


class IFeeScheduleRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[FeeSchedule]: ...
    @abstractmethod
    def get(self, pk: int) -> FeeSchedule | None: ...


class IInvoiceRepository(ABC):
    @abstractmethod
    def scoped(
        self,
        *,
        user,
        roles: set[str],
        permission: str = "finance:read",
    ) -> QuerySet[Invoice]: ...
    @abstractmethod
    def get_scoped(
        self,
        *,
        pk: int,
        user,
        roles: set[str],
        permission: str = "finance:read",
    ) -> Invoice | None: ...


class IStatementExportRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[StatementExport]: ...

    @abstractmethod
    def get(self, pk) -> StatementExport | None: ...


class IDiscountRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[Discount]: ...
    @abstractmethod
    def get(self, pk: int) -> Discount | None: ...


class IPaymentMethodRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[PaymentMethod]: ...
    @abstractmethod
    def get(self, pk: int) -> PaymentMethod | None: ...


class IExpenseRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[Expense]: ...
    @abstractmethod
    def get(self, pk: int) -> Expense | None: ...


class ICashierShiftRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[CashierShift]: ...
