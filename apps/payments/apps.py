from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payments"
    label = "payments"
    verbose_name = "Payments"

    def ready(self) -> None:
        from apps.payments.interfaces.repositories import (
            IPaymentRepository,
            IProviderConfigRepository,
        )
        from apps.payments.interfaces.services import IPaymentService, IProviderConfigService
        from apps.payments.repositories.payment_repository import (
            PaymentRepository,
            ProviderConfigRepository,
        )
        from apps.payments.services.v1.payment_service import PaymentService, ProviderConfigService
        from core.container import container

        container.register(IProviderConfigRepository, ProviderConfigRepository)
        container.register(IPaymentRepository, PaymentRepository)
        container.register(IProviderConfigService, ProviderConfigService)
        container.register(IPaymentService, PaymentService)
