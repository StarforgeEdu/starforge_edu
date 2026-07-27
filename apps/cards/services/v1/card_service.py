"""Cards services — thin orchestration over the preserved domain fns
(issue_card/revoke_card/scan_card/top_up/spend/create_card_type/set_card_type_active)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.cards import services as domain
from apps.cards.dto.card_dto import WalletAmountDTO
from apps.cards.interfaces.repositories import (
    ICardRepository,
    ICardTypeRepository,
    IWalletRepository,
)
from apps.cards.interfaces.services import ICardService, ICardTypeService, IWalletService
from apps.cards.models import Card, CardType, WalletTransaction
from apps.students.models import StudentProfile
from core.exceptions import NotFoundException, PermissionException


class CardTypeService(ICardTypeService):
    def __init__(self, repository: ICardTypeRepository) -> None:
        self.repository = repository

    def list(self) -> QuerySet[CardType]:
        return self.repository.queryset()

    def get(self, *, pk: int) -> CardType | None:
        return self.repository.get(pk=pk)

    def create(self, *, name: str, is_active: bool, created_by) -> CardType:
        card_type = domain.create_card_type(name=name, created_by=created_by)
        if not is_active:  # model default is active; honor an explicit is_active=False
            card_type = domain.set_card_type_active(card_type=card_type, is_active=False)
        return card_type

    def update(self, card_type: CardType, changes: dict[str, Any]) -> CardType:
        return self.repository.apply_changes(card_type, changes=changes)


class CardService(ICardService):
    def __init__(self, repository: ICardRepository) -> None:
        self.repository = repository

    def scoped_list(
        self, *, is_director: bool, is_card_staff: bool, branch_ids: set[int], profile
    ) -> QuerySet[Card]:
        return self.repository.scoped(
            is_director=is_director, is_card_staff=is_card_staff, branch_ids=branch_ids, profile=profile
        )

    def get_visible(
        self, *, is_director: bool, is_card_staff: bool, branch_ids: set[int], profile, pk: int
    ) -> Card | None:
        return self.repository.get_scoped(
            is_director=is_director,
            is_card_staff=is_card_staff,
            branch_ids=branch_ids,
            profile=profile,
            pk=pk,
        )

    def resolve_student(self, *, student_id: int) -> StudentProfile | None:
        return self.repository.get_student(student_id=student_id)

    def resolve_active_card_type(self, *, card_type_id: int):
        # Repos are per-model; the active-type lookup lives on the card-type repo, but the
        # card service only needs the active filter — query it via the model here.
        return CardType.objects.filter(pk=card_type_id, is_active=True).first()

    def issue(self, *, student, card_type: CardType, issued_by) -> Card:
        return domain.issue_card(student=student, card_type=card_type, issued_by=issued_by)

    def revoke(self, card: Card, *, actor, reason: str) -> Card:
        return domain.revoke_card(card_id=card.pk, actor=actor, reason=reason)

    def scan(self, *, code: str, scanned_by) -> dict[str, Any]:
        return domain.scan_card(code=code, scanned_by=scanned_by)


class WalletService(IWalletService):
    def __init__(self, repository: IWalletRepository) -> None:
        self.repository = repository

    def wallet_payload(self, *, student) -> dict[str, Any]:
        wallet = self.repository.get_or_create_for(student=student)
        return {"wallet": wallet, "transactions": self.repository.recent_transactions(wallet=wallet)}

    def get_student_in_scope(self, *, student_id: int, is_director: bool, branch_ids: set[int]):
        student = self.repository.get_student(student_id=student_id)
        if student is None:
            raise NotFoundException(_("Student not found."), code="student_not_found")
        if not is_director and student.branch_id not in branch_ids:
            raise PermissionException(
                _("You can only manage a student in your own branch."), code="branch_out_of_scope"
            )
        return student

    def top_up(self, data: WalletAmountDTO, *, student, actor) -> WalletTransaction:
        return domain.top_up(student=student, amount=data.amount, actor=actor, note=data.note)

    def spend(self, data: WalletAmountDTO, *, student, actor) -> WalletTransaction:
        return domain.spend(student=student, amount=data.amount, actor=actor, note=data.note)
