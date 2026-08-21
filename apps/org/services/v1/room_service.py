"""RoomService — room CRUD (unique branch+name)."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DataError, IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.org.dto.org_dto import RoomCreateDTO
from apps.org.interfaces.repositories import IRoomRepository
from apps.org.interfaces.services import IRoomService
from apps.org.models import Room
from core.exceptions import NotFoundException, ValidationException

_SCALARS = ("name", "capacity", "equipment", "is_active", "notes")


class RoomService(IRoomService):
    def __init__(self, rooms: IRoomRepository) -> None:
        self._rooms = rooms

    def list(self) -> QuerySet[Room]:
        return self._rooms.get_queryset()

    def get(self, room_id: int) -> Room | None:
        return self._rooms.get_by_id(room_id)

    @transaction.atomic
    def create(self, data: RoomCreateDTO) -> Room:
        return self._save(
            Room(
                branch=self._resolve_branch(data.branch_id, for_update=True),
                name=data.name,
                capacity=data.capacity,
                equipment=data.equipment,
                is_active=data.is_active,
                notes=data.notes,
            )
        )

    @transaction.atomic
    def update(self, room: Room, changes: dict[str, Any]) -> Room:
        if "branch" in changes:
            raise ValidationException(
                _("A room cannot be moved with a generic update."),
                code="validation_error",
                fields={"branch": [_("This field is not supported.")]},
            )
        locked = self._rooms.get_queryset().select_for_update(of=("self",)).filter(pk=room.pk).first()
        if locked is None:
            raise NotFoundException(code="not_found")
        room = locked
        for field in _SCALARS:
            if field in changes:
                setattr(room, field, changes[field])
        return self._save(room)

    @transaction.atomic
    def delete(self, room: Room) -> None:
        """Deactivate instead of erasing timetable and occupancy attribution."""
        locked = self._rooms.get_queryset().select_for_update(of=("self",)).filter(pk=room.pk).first()
        if locked is None:
            raise NotFoundException(code="not_found")
        if locked.is_active:
            locked.is_active = False
            locked.save(update_fields=["is_active", "updated_at"])

    @staticmethod
    def _resolve_branch(branch_id: int, *, for_update: bool = False):
        from apps.org.models import Branch

        queryset = Branch.objects.filter(is_active=True, archived_at__isnull=True)
        if for_update:
            queryset = queryset.select_for_update(of=("self",))
        branch = queryset.filter(pk=branch_id).first()
        if branch is None:
            raise ValidationException(
                _("Invalid branch."),
                code="invalid_branch",
                fields={"branch": [_("Choose an active branch.")]},
            )
        return branch

    @staticmethod
    def _save(room: Room) -> Room:
        # Savepoint so a unique-violation rolls back only this write, not the whole
        # (test/request) transaction — else later queries hit a broken transaction.
        try:
            room.full_clean(validate_unique=False, validate_constraints=False)
            with transaction.atomic():
                room.save()
        except DjangoValidationError as exc:
            fields = {
                field: [str(message) for message in messages]
                for field, messages in getattr(exc, "message_dict", {"field": exc.messages}).items()
            }
            raise ValidationException(
                _("Please review the room fields."),
                code="validation_error",
                fields=fields,
            ) from exc
        except IntegrityError as exc:
            raise ValidationException(
                _("A room with this name already exists in the branch."),
                code="validation_error",
                fields={"name": ["Already used in this branch."]},
            ) from exc
        except DataError as exc:  # e.g. capacity out of range -> clean 400, not a 500
            raise ValidationException(_("A field value is out of range."), code="validation_error") from exc
        return room
