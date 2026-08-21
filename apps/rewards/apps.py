from django.apps import AppConfig


class RewardsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.rewards"
    label = "rewards"
    verbose_name = "Staff rewards"

    def ready(self) -> None:
        from apps.rewards.interfaces.repositories import (
            IRewardGrantRepository,
            IRewardTypeRepository,
        )
        from apps.rewards.interfaces.services import IRewardGrantService, IRewardTypeService
        from apps.rewards.repositories.reward_grant_repository import RewardGrantRepository
        from apps.rewards.repositories.reward_type_repository import RewardTypeRepository
        from apps.rewards.services.v1.reward_grant_service import RewardGrantService
        from apps.rewards.services.v1.reward_type_service import RewardTypeService
        from core.container import container

        container.register(IRewardTypeRepository, RewardTypeRepository)
        container.register(IRewardGrantRepository, RewardGrantRepository)
        container.register(IRewardTypeService, RewardTypeService)
        container.register(IRewardGrantService, RewardGrantService)
