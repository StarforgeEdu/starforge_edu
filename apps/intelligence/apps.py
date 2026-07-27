from django.apps import AppConfig


class IntelligenceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.intelligence"
    label = "intelligence"
    verbose_name = "Intelligence & risk flags"

    def ready(self) -> None:
        from apps.intelligence.interfaces.services import IIntelligenceService
        from apps.intelligence.services.v1.intelligence_service import IntelligenceService
        from core.container import container

        container.register(IIntelligenceService, IntelligenceService)
