"""Cards-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.cards.dto.card_dto import WalletAmountDTO
from apps.cards.models import Card, CardType, WalletTransaction
from apps.students.models import StudentProfile


class ICardTypeService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[CardType]: ...

    @abstractmethod
    def get(self, *, pk: int) -> CardType | None: ...

    @abstractmethod
    def create(self, *, name: str, is_active: bool, created_by) -> CardType: ...

    @abstractmethod
    def update(self, card_type: CardType, changes: dict[str, Any]) -> CardType: ...


class ICardService(ABC):
    @abstractmethod
    def scoped_list(
        self, *, is_director: bool, is_card_staff: bool, branch_ids: set[int], profile
    ) -> QuerySet[Card]: ...

    @abstractmethod
    def get_visible(
        self, *, is_director: bool, is_card_staff: bool, branch_ids: set[int], profile, pk: int
    ) -> Card | None: ...

    @abstractmethod
    def resolve_student(self, *, student_id: int) -> StudentProfile | None: ...

    @abstractmethod
    def resolve_active_card_type(self, *, card_type_id: int) -> CardType | None: ...

    @abstractmethod
    def issue(self, *, student, card_type: CardType, issued_by) -> Card: ...

    @abstractmethod
    def revoke(self, card: Card, *, actor, reason: str) -> Card: ...

    @abstractmethod
    def scan(self, *, code: str, scanned_by) -> dict[str, Any]: ...


class IWalletService(ABC):
    @abstractmethod
    def wallet_payload(self, *, student) -> dict[str, Any]: ...

    @abstractmethod
    def get_student_in_scope(self, *, student_id: int, is_director: bool, branch_ids: set[int]): ...

    @abstractmethod
    def top_up(self, data: WalletAmountDTO, *, student, actor) -> WalletTransaction: ...

    @abstractmethod
    def spend(self, data: WalletAmountDTO, *, student, actor) -> WalletTransaction: ...
