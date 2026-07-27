from django.apps import AppConfig


class StudentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.students"
    label = "students"
    verbose_name = "Students"

    def ready(self) -> None:
        from apps.students.interfaces.repositories import (
            IEnrollmentReasonRepository,
            IStudentRepository,
        )
        from apps.students.interfaces.student_service import (
            IEnrollmentReasonService,
            IStudentService,
        )
        from apps.students.repositories.student_repository import (
            EnrollmentReasonRepository,
            StudentRepository,
        )
        from apps.students.services.v1.student_service import (
            EnrollmentReasonService,
            StudentService,
        )
        from core.container import container

        container.register(IStudentRepository, StudentRepository)
        container.register(IEnrollmentReasonRepository, EnrollmentReasonRepository)
        container.register(IStudentService, StudentService)
        container.register(IEnrollmentReasonService, EnrollmentReasonService)
