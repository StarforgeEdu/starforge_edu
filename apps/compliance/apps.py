from django.apps import AppConfig


class ComplianceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.compliance"
    label = "compliance"
    verbose_name = "Compliance"

    def ready(self) -> None:
        from apps.compliance.interfaces.repositories import IPenaltyRepository, IRuleRepository
        from apps.compliance.interfaces.services import IPenaltyService, IRuleService
        from apps.compliance.repositories.compliance_repository import (
            PenaltyRepository,
            RuleRepository,
        )
        from apps.compliance.services.v1.compliance_service import PenaltyService, RuleService
        from core.container import container

        container.register(IRuleRepository, RuleRepository)
        container.register(IPenaltyRepository, PenaltyRepository)
        container.register(IRuleService, RuleService)
        container.register(IPenaltyService, PenaltyService)
