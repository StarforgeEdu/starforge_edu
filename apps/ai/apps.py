from django.apps import AppConfig


class AIConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai"
    label = "ai_app"
    verbose_name = "AI"

    def ready(self) -> None:
        from apps.ai import receivers  # noqa: F401  (connect signal receivers)
        from apps.ai.interfaces.services import IAIService
        from apps.ai.services.v1.ai_service import AIService
        from core.container import container

        container.register(IAIService, AIService)
