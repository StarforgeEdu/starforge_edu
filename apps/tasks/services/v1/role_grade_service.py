"""RoleGradeService — the per-centre role hierarchy (F5-1), a small CRUD table."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.tasks.dto.task_dto import RoleGradeDTO
from apps.tasks.interfaces.repositories import IRoleGradeRepository
from apps.tasks.interfaces.services import IRoleGradeService
from apps.tasks.models import RoleGrade
from core.exceptions import ValidationException
from core.permissions import Role

_SCALARS = ("role", "level", "label")


def _validate_role(role: str) -> None:
    if role not in Role.ALL:  # mirrors the old serializer's validate_role
        raise ValidationException(
            _("Unknown role."), code="validation_error", fields={"role": ["Unknown role."]}
        )


def _assert_role_free(role: str, *, exclude_pk: int | None = None) -> None:
    """400 (field error) if the role already has a grade — restores the unique
    constraint the old ModelSerializer surfaced as a clean 400, not a 500."""
    qs = RoleGrade.objects.filter(role=role)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise ValidationException(
            _("This role already has a grade."),
            code="validation_error",
            fields={"role": ["This role already has a grade."]},
        )


class RoleGradeService(IRoleGradeService):
    def __init__(self, grades: IRoleGradeRepository) -> None:
        self._grades = grades

    def list(self) -> QuerySet[RoleGrade]:
        return self._grades.get_queryset()

    def get(self, pk: int) -> RoleGrade | None:
        return self._grades.get_by_id(pk)

    def create(self, data: RoleGradeDTO) -> RoleGrade:
        _validate_role(data.role)
        _assert_role_free(data.role)
        return RoleGrade.objects.create(role=data.role, level=data.level, label=data.label)

    def update(self, grade: RoleGrade, changes: dict[str, Any]) -> RoleGrade:
        if "role" in changes:
            _validate_role(changes["role"])
            _assert_role_free(changes["role"], exclude_pk=grade.pk)
        for field in _SCALARS:
            if field in changes:
                setattr(grade, field, changes[field])
        grade.save()
        return grade

    def delete(self, grade: RoleGrade) -> None:
        grade.delete()
