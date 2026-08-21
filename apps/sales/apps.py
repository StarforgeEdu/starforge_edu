from django.apps import AppConfig


class SalesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sales"

    def ready(self) -> None:
        from apps.sales.interfaces.repositories import ISaleRepository
        from apps.sales.interfaces.services import ISaleService
        from apps.sales.repositories.sale_repository import SaleRepository
        from apps.sales.services.v1.sale_service import SaleService
        from core.container import container

        container.register(ISaleRepository, SaleRepository)
        container.register(ISaleService, SaleService)
