from django.apps import AppConfig


class FinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.finance"
    label = "finance"
    verbose_name = "Finance"

    def ready(self) -> None:
        from apps.finance.interfaces.repositories import (
            ICashierShiftRepository,
            IDiscountRepository,
            IExpenseRepository,
            IFeeScheduleRepository,
            IInvoiceRepository,
            IPaymentMethodRepository,
        )
        from apps.finance.interfaces.services import IFinanceService
        from apps.finance.repositories.finance_repository import (
            CashierShiftRepository,
            DiscountRepository,
            ExpenseRepository,
            FeeScheduleRepository,
            InvoiceRepository,
            PaymentMethodRepository,
        )
        from apps.finance.services.v1.finance_service import FinanceService
        from core.container import container

        from . import receivers  # noqa: F401

        container.register(IFeeScheduleRepository, FeeScheduleRepository)
        container.register(IInvoiceRepository, InvoiceRepository)
        container.register(IDiscountRepository, DiscountRepository)
        container.register(IPaymentMethodRepository, PaymentMethodRepository)
        container.register(IExpenseRepository, ExpenseRepository)
        container.register(ICashierShiftRepository, CashierShiftRepository)
        container.register(IFinanceService, FinanceService)
