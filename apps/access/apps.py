from django.apps import AppConfig


class AccessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.access"
    label = "access"
    verbose_name = "Access & permissions"

    def ready(self) -> None:
        from apps.access.interfaces.repositories import IOverrideRepository
        from apps.access.interfaces.services import IAccessService
        from apps.access.repositories.override_repository import OverrideRepository
        from apps.access.services.v1.access_service import AccessService
        from core.container import container

        container.register(IOverrideRepository, OverrideRepository)
        container.register(IAccessService, AccessService)
