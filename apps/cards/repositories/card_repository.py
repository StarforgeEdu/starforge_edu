"""ORM-backed cards repositories (card types, cards with role/branch scoping, wallets)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.cards.interfaces.repositories import (
    ICardRepository,
    ICardTypeRepository,
    IWalletRepository,
)
from apps.cards.models import Card, CardType, Wallet, WalletTransaction
from apps.students.models import StudentProfile
from core.repositories import BaseRepository


class CardTypeRepository(BaseRepository[CardType], ICardTypeRepository):
    model = CardType

    def queryset(self) -> QuerySet[CardType]:
        return CardType.objects.select_related("created_by").all()

    def get(self, *, pk: int) -> CardType | None:
        return self.queryset().filter(pk=pk).first()

    def active(self, *, pk: int) -> CardType | None:
        return CardType.objects.filter(pk=pk, is_active=True).first()

    def apply_changes(self, card_type: CardType, *, changes: dict[str, Any]) -> CardType:
        for field, value in changes.items():
            setattr(card_type, field, value)
        if changes:
            card_type.save(update_fields=[*changes.keys(), "updated_at"])
        return card_type


class CardRepository(BaseRepository[Card], ICardRepository):
    model = Card

    def _base(self) -> QuerySet[Card]:
        return Card.objects.select_related("student", "student__user", "card_type", "issued_by")

    def scoped(
        self, *, is_director: bool, is_card_staff: bool, branch_ids: set[int], profile
    ) -> QuerySet[Card]:
        qs = self._base()
        if is_director:
            return qs
        if is_card_staff:  # card:write or card:scan -> their branch's cards
            return qs.filter(student__branch_id__in=branch_ids)
        # a student sees only their own card(s)
        return qs.filter(student=profile) if profile is not None else qs.none()

    def get_scoped(
        self, *, is_director: bool, is_card_staff: bool, branch_ids: set[int], profile, pk: int
    ) -> Card | None:
        return (
            self.scoped(
                is_director=is_director, is_card_staff=is_card_staff, branch_ids=branch_ids, profile=profile
            )
            .filter(pk=pk)
            .first()
        )

    def get_student(self, *, student_id: int) -> StudentProfile | None:
        return StudentProfile.objects.select_related("branch").filter(pk=student_id).first()


class WalletRepository(BaseRepository[Wallet], IWalletRepository):
    model = Wallet

    def get_or_create_for(self, *, student) -> Wallet:
        wallet, _created = Wallet.objects.get_or_create(student=student)
        return wallet

    def recent_transactions(self, *, wallet: Wallet, limit: int = 50) -> list[WalletTransaction]:
        return list(wallet.transactions.all()[:limit])

    def get_student(self, *, student_id: int) -> StudentProfile | None:
        return StudentProfile.objects.select_related("branch").filter(pk=student_id).first()
