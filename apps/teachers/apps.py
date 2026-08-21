from django.apps import AppConfig


class TeachersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.teachers"
    label = "teachers"
    verbose_name = "Teachers"

    def ready(self) -> None:
        from apps.teachers.interfaces.repositories import ITeacherRepository
        from apps.teachers.interfaces.teacher_service import ITeacherService
        from apps.teachers.repositories.teacher_repository import TeacherRepository
        from apps.teachers.services.v1.teacher_service import TeacherService
        from core.container import container

        container.register(ITeacherRepository, TeacherRepository)
        container.register(ITeacherService, TeacherService)
