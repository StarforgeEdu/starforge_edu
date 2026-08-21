"""ORM-backed room repository."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.org.interfaces.repositories import IRoomRepository
from apps.org.models import Room
from core.repositories import BaseRepository


class RoomRepository(BaseRepository[Room], IRoomRepository):
    model = Room

    def get_queryset(self) -> QuerySet[Room]:
        return Room.objects.select_related("branch")
