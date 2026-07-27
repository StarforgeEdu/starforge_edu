from django.apps import AppConfig


class BillingConfig(AppConfig):
    """Platform monetization (TD-8). PUBLIC-schema only — registered in
    SHARED_APPS, never TENANT_APPS. Plans/Subscriptions/UsageSnapshots all
    live in the public schema (one row per Center)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
    label = "billing"
    verbose_name = "Billing & Paywall"

    def ready(self) -> None:
        from apps.billing.interfaces.repositories import IPlanRepository, ISubscriptionRepository
        from apps.billing.interfaces.services import IBillingService
        from apps.billing.repositories.plan_repository import PlanRepository
        from apps.billing.repositories.subscription_repository import SubscriptionRepository
        from apps.billing.services.v1.billing_service import BillingService
        from core.container import container

        from . import receivers  # noqa: F401

        container.register(IPlanRepository, PlanRepository)
        container.register(ISubscriptionRepository, SubscriptionRepository)
        container.register(IBillingService, BillingService)
