from django.apps import AppConfig


class CRMConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.crm"
    verbose_name = "Admissions CRM"

    def ready(self) -> None:
        from apps.crm import receivers  # noqa: F401
        from apps.crm.interfaces.repositories import ICRMRepository
        from apps.crm.interfaces.services import ICRMService
        from apps.crm.repositories.crm_repository import CRMRepository
        from apps.crm.services.v1.crm_service import CRMService
        from core.container import container

        container.register(ICRMRepository, CRMRepository)
        container.register(ICRMService, CRMService)
