"""RoomService — room CRUD (unique branch+name)."""

from __future__ import annotations

from typing import Any

from django.db import DataError, IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.org.dto.org_dto import RoomCreateDTO
from apps.org.interfaces.repositories import IRoomRepository
from apps.org.interfaces.services import IRoomService
from apps.org.models import Room
from core.exceptions import ValidationException

_SCALARS = ("name", "capacity", "equipment", "is_active", "notes")


class RoomService(IRoomService):
    def __init__(self, rooms: IRoomRepository) -> None:
        self._rooms = rooms

    def list(self) -> QuerySet[Room]:
        return self._rooms.get_queryset()

    def get(self, room_id: int) -> Room | None:
        return self._rooms.get_by_id(room_id)

    def create(self, data: RoomCreateDTO) -> Room:
        return self._save(
            Room(
                branch=self._resolve_branch(data.branch_id),
                name=data.name,
                capacity=data.capacity,
                equipment=data.equipment,
                is_active=data.is_active,
                notes=data.notes,
            )
        )

    def update(self, room: Room, changes: dict[str, Any]) -> Room:
        if "branch" in changes:
            room.branch = self._resolve_branch(changes["branch"])
        for field in _SCALARS:
            if field in changes:
                setattr(room, field, changes[field])
        return self._save(room)

    def delete(self, room: Room) -> None:
        self._rooms.delete(room)

    @staticmethod
    def _resolve_branch(branch_id: int):
        from apps.org.models import Branch

        branch = Branch.objects.filter(pk=branch_id).first()
        if branch is None:
            raise ValidationException(
                _("Invalid branch."), code="invalid_branch", fields={"branch": ["Not found."]}
            )
        return branch

    @staticmethod
    def _save(room: Room) -> Room:
        # Savepoint so a unique-violation rolls back only this write, not the whole
        # (test/request) transaction — else later queries hit a broken transaction.
        try:
            with transaction.atomic():
                room.save()
        except IntegrityError as exc:
            raise ValidationException(
                _("A room with this name already exists in the branch."),
                code="validation_error",
                fields={"name": ["Already used in this branch."]},
            ) from exc
        except DataError as exc:  # e.g. capacity out of range -> clean 400, not a 500
            raise ValidationException(
                _("A field value is out of range."), code="validation_error"
            ) from exc
        return room
