from django.apps import AppConfig


class CampaignsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.campaigns"

    def ready(self) -> None:
        from apps.campaigns.interfaces.repositories import (
            ICampaignRepository,
            IDoNotContactRepository,
            ITemplateRepository,
        )
        from apps.campaigns.interfaces.services import (
            ICampaignService,
            IDoNotContactService,
            ITemplateService,
        )
        from apps.campaigns.repositories.campaign_repository import CampaignRepository
        from apps.campaigns.repositories.do_not_contact_repository import DoNotContactRepository
        from apps.campaigns.repositories.template_repository import TemplateRepository
        from apps.campaigns.services.v1.campaign_service import CampaignService
        from apps.campaigns.services.v1.do_not_contact_service import DoNotContactService
        from apps.campaigns.services.v1.template_service import TemplateService
        from core.container import container

        container.register(ICampaignRepository, CampaignRepository)
        container.register(IDoNotContactRepository, DoNotContactRepository)
        container.register(ITemplateRepository, TemplateRepository)
        container.register(ICampaignService, CampaignService)
        container.register(IDoNotContactService, DoNotContactService)
        container.register(ITemplateService, TemplateService)
