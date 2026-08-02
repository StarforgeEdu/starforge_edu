from django.apps import AppConfig


class PayrollConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payroll"
    label = "payroll"
    verbose_name = "Payroll"

    def ready(self) -> None:
        from apps.payroll.interfaces.repositories import IPayrollRepository
        from apps.payroll.interfaces.services import IPayrollService
        from apps.payroll.repositories.payroll_repository import PayrollRepository
        from apps.payroll.services.v1.payroll_service import PayrollService
        from core.container import container

        container.register(IPayrollRepository, PayrollRepository)
        container.register(IPayrollService, PayrollService)
