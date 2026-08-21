from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.attendance"
    label = "attendance"
    verbose_name = "Attendance"

    def ready(self) -> None:
        from apps.attendance.interfaces.repositories import IAttendanceRepository
        from apps.attendance.interfaces.services import IAttendanceService
        from apps.attendance.repositories.attendance_repository import AttendanceRepository
        from apps.attendance.services.v1.attendance_service import AttendanceService
        from core.container import container

        container.register(IAttendanceRepository, AttendanceRepository)
        container.register(IAttendanceService, AttendanceService)
