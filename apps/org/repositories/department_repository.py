"""ORM-backed department repository."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.org.interfaces.repositories import IDepartmentRepository
from apps.org.models import Department
from core.repositories import BaseRepository


class DepartmentRepository(BaseRepository[Department], IDepartmentRepository):
    model = Department

    def get_queryset(self) -> QuerySet[Department]:
        return Department.objects.select_related("branch", "head__teacher_profile")
