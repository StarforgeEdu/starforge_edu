"""Cards-domain repository ports."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.cards.models import Card, CardType, Wallet
from apps.students.models import StudentProfile
from core.interfaces import IBaseRepository


class ICardTypeRepository(IBaseRepository[CardType]):
    def queryset(self) -> QuerySet[CardType]:
        raise NotImplementedError

    def get(self, *, pk: int) -> CardType | None:
        raise NotImplementedError

    def active(self, *, pk: int) -> CardType | None:
        raise NotImplementedError

    def apply_changes(self, card_type: CardType, *, changes: dict) -> CardType:
        raise NotImplementedError


class ICardRepository(IBaseRepository[Card]):
    def scoped(
        self, *, is_director: bool, is_card_staff: bool, branch_ids: set[int], profile
    ) -> QuerySet[Card]:
        raise NotImplementedError

    def get_scoped(
        self, *, is_director: bool, is_card_staff: bool, branch_ids: set[int], profile, pk: int
    ) -> Card | None:
        raise NotImplementedError

    def get_student(self, *, student_id: int) -> StudentProfile | None:
        raise NotImplementedError


class IWalletRepository(IBaseRepository[Wallet]):
    def get_or_create_for(self, *, student) -> Wallet:
        raise NotImplementedError

    def recent_transactions(self, *, wallet: Wallet, limit: int = 50) -> list:
        raise NotImplementedError

    def get_student(self, *, student_id: int) -> StudentProfile | None:
        raise NotImplementedError
