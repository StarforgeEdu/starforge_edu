from django.apps import AppConfig


class OrgConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.org"
    label = "org"
    verbose_name = "Organization"

    def ready(self) -> None:
        from apps.org.interfaces.repositories import (
            IBranchRepository,
            IBranchTransferRepository,
            IDepartmentRepository,
            IRoomRepository,
        )
        from apps.org.interfaces.services import (
            IBranchService,
            IBranchTransferService,
            ICenterSettingsService,
            IDepartmentService,
            IRoomService,
        )
        from apps.org.repositories.branch_repository import BranchRepository
        from apps.org.repositories.department_repository import DepartmentRepository
        from apps.org.repositories.room_repository import RoomRepository
        from apps.org.repositories.transfer_repository import BranchTransferRepository
        from apps.org.services.v1.branch_service import BranchService
        from apps.org.services.v1.department_service import DepartmentService
        from apps.org.services.v1.room_service import RoomService
        from apps.org.services.v1.settings_service import CenterSettingsService
        from apps.org.services.v1.transfer_service import BranchTransferService
        from core.container import container

        from . import receivers  # noqa: F401

        container.register(IBranchRepository, BranchRepository)
        container.register(IDepartmentRepository, DepartmentRepository)
        container.register(IRoomRepository, RoomRepository)
        container.register(IBranchTransferRepository, BranchTransferRepository)
        container.register(IBranchService, BranchService)
        container.register(IDepartmentService, DepartmentService)
        container.register(IRoomService, RoomService)
        container.register(IBranchTransferService, BranchTransferService)
        container.register(ICenterSettingsService, CenterSettingsService)
