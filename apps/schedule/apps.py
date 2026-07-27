from django.apps import AppConfig


class ScheduleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.schedule"
    label = "schedule"
    verbose_name = "Schedule"

    def ready(self) -> None:
        from apps.schedule.interfaces.repositories import (
            ILessonRepository,
            ILessonTypeRepository,
            IRecurrenceRuleRepository,
            ITermRepository,
            ITimeSlotRepository,
        )
        from apps.schedule.interfaces.services import (
            ILessonService,
            ILessonTypeService,
            IRecurrenceRuleService,
            ITermService,
            ITimeSlotService,
        )
        from apps.schedule.repositories.schedule_repository import (
            LessonRepository,
            LessonTypeRepository,
            RecurrenceRuleRepository,
            TermRepository,
            TimeSlotRepository,
        )
        from apps.schedule.services.v1.schedule_service import (
            LessonService,
            LessonTypeService,
            RecurrenceRuleService,
            TermService,
            TimeSlotService,
        )
        from core.container import container

        container.register(ITermRepository, TermRepository)
        container.register(ITimeSlotRepository, TimeSlotRepository)
        container.register(ILessonTypeRepository, LessonTypeRepository)
        container.register(IRecurrenceRuleRepository, RecurrenceRuleRepository)
        container.register(ILessonRepository, LessonRepository)
        container.register(ITermService, TermService)
        container.register(ITimeSlotService, TimeSlotService)
        container.register(ILessonTypeService, LessonTypeService)
        container.register(IRecurrenceRuleService, RecurrenceRuleService)
        container.register(ILessonService, LessonService)
