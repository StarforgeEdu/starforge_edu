from django.apps import AppConfig


class LoansConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.loans"

    def ready(self) -> None:
        from apps.loans.interfaces.repositories import ILoanRepository
        from apps.loans.interfaces.services import ILoanService
        from apps.loans.repositories.loan_repository import LoanRepository
        from apps.loans.services.v1.loan_service import LoanService
        from core.container import container

        container.register(ILoanRepository, LoanRepository)
        container.register(ILoanService, LoanService)
