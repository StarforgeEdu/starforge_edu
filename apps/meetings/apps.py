from django.apps import AppConfig


class MeetingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.meetings"

    def ready(self) -> None:
        from apps.meetings.interfaces.repositories import IMeetingRepository
        from apps.meetings.interfaces.services import IMeetingService
        from apps.meetings.repositories.meeting_repository import MeetingRepository
        from apps.meetings.services.v1.meeting_service import MeetingService
        from core.container import container

        container.register(IMeetingRepository, MeetingRepository)
        container.register(IMeetingService, MeetingService)
