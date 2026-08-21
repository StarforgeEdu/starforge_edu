from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    label = "notifications"
    verbose_name = "Notifications"

    def ready(self) -> None:
        # Connect every Day-1/2/3 source-signal receiver (D3-C-4). Import here so
        # the signal handlers register exactly once at app load.
        from apps.notifications.interfaces.repositories import (
            INotificationRepository,
            INotificationTemplateRepository,
        )
        from apps.notifications.interfaces.services import (
            INotificationService,
            INotificationTemplateService,
        )
        from apps.notifications.repositories.notification_repository import (
            NotificationRepository,
            NotificationTemplateRepository,
        )
        from apps.notifications.services.v1.notification_service import (
            NotificationService,
            NotificationTemplateService,
        )
        from core.container import container

        from . import receivers  # noqa: F401

        container.register(INotificationRepository, NotificationRepository)
        container.register(INotificationTemplateRepository, NotificationTemplateRepository)
        container.register(INotificationService, NotificationService)
        container.register(INotificationTemplateService, NotificationTemplateService)
