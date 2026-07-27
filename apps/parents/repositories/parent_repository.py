"""ORM-backed parent repository — bakes in select_related and the role scoping."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.parents.interfaces.repositories import IParentRepository
from apps.parents.models import ParentProfile
from apps.parents.repositories.scoping import scope_rows
from core.repositories import BaseRepository


class ParentRepository(BaseRepository[ParentProfile], IParentRepository):
    model = ParentProfile

    def get_queryset(self) -> QuerySet[ParentProfile]:
        return ParentProfile.objects.select_related("user")

    def scoped(self, *, user, roles) -> QuerySet[ParentProfile]:
        return scope_rows(self.get_queryset(), user=user, roles=roles, own_filter={"user": user})

    def get_scoped(self, *, user, roles, pk: int) -> ParentProfile | None:
        return self.scoped(user=user, roles=roles).filter(pk=pk).first()

    def profile_for(self, user) -> ParentProfile | None:
        return self.get_queryset().filter(user=user).first()

    def students_for(self, parent) -> QuerySet:
        # The sanctioned parents->students link (Guardian). select_related the
        # relations the student presenter reads so a family list is not N+1.
        from apps.students.models import StudentProfile

        return (
            StudentProfile.objects.filter(guardians__parent=parent)
            .select_related("user", "branch")
            .distinct()
        )
