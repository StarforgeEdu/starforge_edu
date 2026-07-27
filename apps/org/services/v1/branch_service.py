"""BranchService — branch CRUD (soft-delete), weekly-hours replace, holidays."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.db import DataError, IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.org.dto.org_dto import BranchCreateDTO, HolidayCreateDTO, WorkingHourDTO
from apps.org.interfaces.repositories import IBranchRepository
from apps.org.interfaces.services import IBranchService
from apps.org.models import Branch, BranchHoliday, BranchWorkingHours
from core.exceptions import ConflictException, NotFoundException, ValidationException

_SCALARS = ("name", "slug", "address", "phone", "timezone", "is_active", "max_students", "max_teachers")


class BranchService(IBranchService):
    def __init__(self, branches: IBranchRepository) -> None:
        self._branches = branches

    def list(self) -> QuerySet[Branch]:
        return self._branches.active()

    def get(self, branch_id: int) -> Branch | None:
        # Archived branches are excluded from the writable/detail surface (D1-LF-7).
        return self._branches.active().filter(pk=branch_id).first()

    def create(self, data: BranchCreateDTO) -> Branch:
        return self._save(
            Branch(
                name=data.name,
                slug=data.slug,
                address=data.address,
                phone=data.phone,
                timezone=data.timezone,
                is_active=data.is_active,
                max_students=data.max_students,
                max_teachers=data.max_teachers,
            )
        )

    def update(self, branch: Branch, changes: dict[str, Any]) -> Branch:
        for field in _SCALARS:
            if field in changes:
                setattr(branch, field, changes[field])
        return self._save(branch)

    def archive(self, branch: Branch) -> None:
        from apps.org.services import archive_branch

        archive_branch(branch)  # 409 branch_has_active_students if occupied

    def replace_working_hours(
        self, branch: Branch, rows: Sequence[WorkingHourDTO]
    ) -> Sequence[BranchWorkingHours]:
        from apps.org.services import replace_working_hours

        return replace_working_hours(
            branch,
            [
                {
                    "weekday": r.weekday,
                    "opens_at": r.opens_at,
                    "closes_at": r.closes_at,
                    "is_closed": r.is_closed,
                }
                for r in rows
            ],
        )

    def list_holidays(self, branch: Branch) -> QuerySet[BranchHoliday]:
        return branch.holidays.all()

    def add_holiday(self, branch: Branch, data: HolidayCreateDTO) -> BranchHoliday:
        # Pre-check the (branch, date) unique constraint for a clean 409 instead of
        # a 500 IntegrityError (D1-LF-3); the DB constraint is the TOCTOU backstop.
        if branch.holidays.filter(date=data.date).exists():
            raise ConflictException(
                _("This branch already has a holiday on that date."), code="holiday_exists"
            )
        try:
            with transaction.atomic():  # savepoint for the TOCTOU race on (branch, date)
                return branch.holidays.create(
                    date=data.date,
                    name=data.name,
                    is_working_day_override=data.is_working_day_override,
                )
        except IntegrityError as exc:
            raise ConflictException(
                _("This branch already has a holiday on that date."), code="holiday_exists"
            ) from exc

    def delete_holiday(self, branch: Branch, holiday_id: int) -> None:
        holiday = branch.holidays.filter(pk=holiday_id).first()
        if holiday is None:
            raise NotFoundException(code="not_found")
        holiday.delete()

    @staticmethod
    def _save(branch: Branch) -> Branch:
        try:
            with transaction.atomic():  # savepoint: unique-slug violation must not poison the txn
                branch.save()
        except IntegrityError as exc:
            raise ValidationException(
                _("A branch with this slug already exists."),
                code="validation_error",
                fields={"slug": ["Already used."]},
            ) from exc
        except DataError as exc:  # e.g. max_students out of range -> clean 400, not a 500
            raise ValidationException(
                _("A field value is out of range."), code="validation_error"
            ) from exc
        return branch
