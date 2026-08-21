from django.apps import AppConfig


class ProcurementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.procurement"

    def ready(self) -> None:
        from apps.procurement.interfaces.repositories import IPurchaseOrderRepository
        from apps.procurement.interfaces.services import IPurchaseOrderService
        from apps.procurement.repositories.purchase_order_repository import PurchaseOrderRepository
        from apps.procurement.services.v1.purchase_order_service import PurchaseOrderService
        from core.container import container

        container.register(IPurchaseOrderRepository, PurchaseOrderRepository)
        container.register(IPurchaseOrderService, PurchaseOrderService)
