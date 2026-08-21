from django.apps import AppConfig


class CoversConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.covers"
    label = "covers"
    verbose_name = "Lesson covers"

    def ready(self) -> None:
        from apps.covers.interfaces.repositories import ICoverRepository
        from apps.covers.interfaces.services import ICoverService
        from apps.covers.repositories.cover_repository import CoverRepository
        from apps.covers.services.v1.cover_service import CoverService
        from core.container import container

        container.register(ICoverRepository, CoverRepository)
        container.register(ICoverService, CoverService)
