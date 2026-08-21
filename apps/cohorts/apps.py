from django.apps import AppConfig


class CohortsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cohorts"
    label = "cohorts"
    verbose_name = "Cohorts (class groups)"

    def ready(self) -> None:
        from apps.cohorts.interfaces.cohort_service import ICohortService
        from apps.cohorts.interfaces.repositories import ICohortRepository
        from apps.cohorts.repositories.cohort_repository import CohortRepository
        from apps.cohorts.services.v1.cohort_service import CohortService
        from core.container import container

        from . import receivers  # noqa: F401

        container.register(ICohortRepository, CohortRepository)
        container.register(ICohortService, CohortService)
