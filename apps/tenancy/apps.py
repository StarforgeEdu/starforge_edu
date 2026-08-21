from django.apps import AppConfig


class TenancyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenancy"
    label = "tenancy"
    verbose_name = "Tenancy"

    def ready(self) -> None:
        from apps.tenancy.interfaces.repositories import ICenterRepository
        from apps.tenancy.interfaces.services import ICenterService
        from apps.tenancy.repositories.center_repository import CenterRepository
        from apps.tenancy.services.v1.center_service import CenterService
        from core.container import container

        container.register(ICenterRepository, CenterRepository)
        container.register(ICenterService, CenterService)
