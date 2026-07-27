from django.apps import AppConfig


class AssignmentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.assignments"
    label = "assignments"
    verbose_name = "Assignments (homework)"

    def ready(self) -> None:
        from apps.assignments.interfaces.repositories import (
            IAssignmentRepository,
            ISubmissionRepository,
        )
        from apps.assignments.interfaces.services import IAssignmentService, ISubmissionService
        from apps.assignments.repositories.assignment_repository import (
            AssignmentRepository,
            SubmissionRepository,
        )
        from apps.assignments.services.v1.assignment_service import AssignmentService
        from apps.assignments.services.v1.submission_service import SubmissionService
        from core.container import container

        container.register(IAssignmentRepository, AssignmentRepository)
        container.register(ISubmissionRepository, SubmissionRepository)
        container.register(IAssignmentService, AssignmentService)
        container.register(ISubmissionService, SubmissionService)
