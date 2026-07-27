from django.apps import AppConfig


class MessagingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.messaging"
    label = "messaging"
    verbose_name = "In-app messaging"

    def ready(self) -> None:
        from apps.messaging.interfaces.repositories import IThreadRepository
        from apps.messaging.interfaces.services import IThreadService
        from apps.messaging.repositories.thread_repository import ThreadRepository
        from apps.messaging.services.v1.thread_service import ThreadService
        from core.container import container

        container.register(IThreadRepository, ThreadRepository)
        container.register(IThreadService, ThreadService)
