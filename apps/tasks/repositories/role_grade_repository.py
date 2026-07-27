"""ORM-backed role-grade repository (unscoped centre-wide hierarchy)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.tasks.interfaces.repositories import IRoleGradeRepository
from apps.tasks.models import RoleGrade
from core.repositories import BaseRepository


class RoleGradeRepository(BaseRepository[RoleGrade], IRoleGradeRepository):
    model = RoleGrade

    def get_queryset(self) -> QuerySet[RoleGrade]:
        return RoleGrade.objects.all()
