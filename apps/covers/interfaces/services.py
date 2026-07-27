"""Cover-request service port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.covers.dto.cover_dto import CreateCoverDTO
from apps.covers.models import CoverRequest


class ICoverService(ABC):
    @abstractmethod
    def scoped_list(
        self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int]
    ) -> QuerySet[CoverRequest]: ...

    @abstractmethod
    def get_visible(
        self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int], pk: int
    ) -> CoverRequest | None: ...

    @abstractmethod
    def create(self, data: CreateCoverDTO, *, requester) -> CoverRequest: ...

    @abstractmethod
    def assign(self, *, cover_id: int, cover_teacher_id: int, actor) -> CoverRequest: ...

    @abstractmethod
    def open_pool(self, *, cover_id: int, actor) -> CoverRequest: ...

    @abstractmethod
    def claim(self, *, cover_id: int, claimer_user, actor) -> CoverRequest: ...

    @abstractmethod
    def cancel(self, *, cover_id: int, actor) -> CoverRequest: ...

    @abstractmethod
    def reject(self, *, cover_id: int, actor) -> CoverRequest: ...
