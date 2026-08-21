from django.apps import AppConfig


class TasksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tasks"
    label = "staff_tasks"  # avoid the generic "tasks" label colliding with Celery modules
    verbose_name = "Tasks & role hierarchy"

    def ready(self) -> None:
        from apps.tasks.interfaces.repositories import IRoleGradeRepository, ITaskRepository
        from apps.tasks.interfaces.services import IRoleGradeService, ITaskService
        from apps.tasks.repositories.role_grade_repository import RoleGradeRepository
        from apps.tasks.repositories.task_repository import TaskRepository
        from apps.tasks.services.v1.role_grade_service import RoleGradeService
        from apps.tasks.services.v1.task_service import TaskService
        from core.container import container

        container.register(ITaskRepository, TaskRepository)
        container.register(IRoleGradeRepository, RoleGradeRepository)
        container.register(ITaskService, TaskService)
        container.register(IRoleGradeService, RoleGradeService)
