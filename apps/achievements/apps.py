from django.apps import AppConfig


class AchievementsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.achievements"
    label = "achievements"
    verbose_name = "Achievements"

    def ready(self) -> None:
        from apps.achievements.interfaces.repositories import (
            IAchievementGrantRepository,
            IAchievementRepository,
        )
        from apps.achievements.interfaces.services import IAchievementService
        from apps.achievements.repositories.achievement_grant_repository import (
            AchievementGrantRepository,
        )
        from apps.achievements.repositories.achievement_repository import AchievementRepository
        from apps.achievements.services.v1.achievement_service import AchievementService
        from core.container import container

        container.register(IAchievementRepository, AchievementRepository)
        container.register(IAchievementGrantRepository, AchievementGrantRepository)
        container.register(IAchievementService, AchievementService)
