"""Org-domain service ports — one per endpoint aggregate. Views resolve these
from the container."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from django.db.models import QuerySet

from apps.org.dto.org_dto import (
    BranchCreateDTO,
    DepartmentCreateDTO,
    HolidayCreateDTO,
    RoomCreateDTO,
    WorkingHourDTO,
)
from apps.org.models import (
    Branch,
    BranchHoliday,
    BranchTransfer,
    BranchWorkingHours,
    CenterSettings,
    Department,
    Room,
)


class IBranchService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[Branch]: ...

    @abstractmethod
    def get(self, branch_id: int) -> Branch | None: ...

    @abstractmethod
    def create(self, data: BranchCreateDTO) -> Branch: ...

    @abstractmethod
    def update(self, branch: Branch, changes: dict[str, Any]) -> Branch: ...

    @abstractmethod
    def archive(self, branch: Branch) -> None: ...

    @abstractmethod
    def replace_working_hours(
        self, branch: Branch, rows: Sequence[WorkingHourDTO]
    ) -> Sequence[BranchWorkingHours]:
        # Sequence, not list[...]: the `list` method above shadows the builtin in
        # this class's annotation scope.
        ...

    @abstractmethod
    def list_holidays(self, branch: Branch) -> QuerySet[BranchHoliday]: ...

    @abstractmethod
    def add_holiday(self, branch: Branch, data: HolidayCreateDTO) -> BranchHoliday: ...

    @abstractmethod
    def delete_holiday(self, branch: Branch, holiday_id: int) -> None: ...


class IDepartmentService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[Department]: ...

    @abstractmethod
    def get(self, department_id: int) -> Department | None: ...

    @abstractmethod
    def create(self, data: DepartmentCreateDTO) -> Department: ...

    @abstractmethod
    def update(self, department: Department, changes: dict[str, Any]) -> Department: ...

    @abstractmethod
    def delete(self, department: Department) -> None: ...


class IRoomService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[Room]: ...

    @abstractmethod
    def get(self, room_id: int) -> Room | None: ...

    @abstractmethod
    def create(self, data: RoomCreateDTO) -> Room: ...

    @abstractmethod
    def update(self, room: Room, changes: dict[str, Any]) -> Room: ...

    @abstractmethod
    def delete(self, room: Room) -> None: ...


class IBranchTransferService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[BranchTransfer]: ...

    @abstractmethod
    def get(self, transfer_id: int) -> BranchTransfer | None: ...


class ICenterSettingsService(ABC):
    @abstractmethod
    def read(self) -> CenterSettings: ...

    @abstractmethod
    def update(self, changes: dict[str, Any]) -> CenterSettings: ...
