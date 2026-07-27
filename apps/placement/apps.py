from django.apps import AppConfig


class PlacementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.placement"
    verbose_name = "Placement tests"

    def ready(self) -> None:
        from apps.placement.interfaces.repositories import (
            IGroupProposalRepository,
            IPlacementAttemptRepository,
            IPlacementTestRepository,
        )
        from apps.placement.interfaces.services import IPlacementService
        from apps.placement.repositories.placement_repository import (
            GroupProposalRepository,
            PlacementAttemptRepository,
            PlacementTestRepository,
        )
        from apps.placement.services.v1.placement_service import PlacementService
        from core.container import container

        container.register(IPlacementTestRepository, PlacementTestRepository)
        container.register(IPlacementAttemptRepository, PlacementAttemptRepository)
        container.register(IGroupProposalRepository, GroupProposalRepository)
        container.register(IPlacementService, PlacementService)
