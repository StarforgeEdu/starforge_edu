from django.apps import AppConfig


class ParentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.parents"
    label = "parents"
    verbose_name = "Parents"

    def ready(self) -> None:
        from apps.parents.interfaces.repositories import (
            IGuardianRepository,
            IParentRepository,
            IPickupRepository,
        )
        from apps.parents.interfaces.services import (
            IGuardianService,
            IParentService,
            IPickupService,
        )
        from apps.parents.repositories.guardian_repository import GuardianRepository
        from apps.parents.repositories.parent_repository import ParentRepository
        from apps.parents.repositories.pickup_repository import PickupRepository
        from apps.parents.services.v1.guardian_service import GuardianService
        from apps.parents.services.v1.parent_service import ParentService
        from apps.parents.services.v1.pickup_service import PickupService
        from core.container import container

        container.register(IParentRepository, ParentRepository)
        container.register(IGuardianRepository, GuardianRepository)
        container.register(IPickupRepository, PickupRepository)
        container.register(IParentService, ParentService)
        container.register(IGuardianService, GuardianService)
        container.register(IPickupService, PickupService)
