from django.apps import AppConfig


class CardsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cards"

    def ready(self) -> None:
        from apps.cards.interfaces.repositories import (
            ICardRepository,
            ICardTypeRepository,
            IWalletRepository,
        )
        from apps.cards.interfaces.services import ICardService, ICardTypeService, IWalletService
        from apps.cards.repositories.card_repository import (
            CardRepository,
            CardTypeRepository,
            WalletRepository,
        )
        from apps.cards.services.v1.card_service import CardService, CardTypeService, WalletService
        from core.container import container

        container.register(ICardTypeRepository, CardTypeRepository)
        container.register(ICardRepository, CardRepository)
        container.register(IWalletRepository, WalletRepository)
        container.register(ICardTypeService, CardTypeService)
        container.register(ICardService, CardService)
        container.register(IWalletService, WalletService)
