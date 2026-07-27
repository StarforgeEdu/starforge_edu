"""ORM-backed teacher repository — bakes in select_related so list/detail never N+1."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.teachers.interfaces.repositories import ITeacherRepository
from apps.teachers.models import TeacherProfile
from core.repositories import BaseRepository


class TeacherRepository(BaseRepository[TeacherProfile], ITeacherRepository):
    model = TeacherProfile

    def get_queryset(self) -> QuerySet[TeacherProfile]:
        # user/branch/department are read by the presenter on every row — eager-load
        # them so a page of N teachers is 1 query, not 1 + 3N.
        return TeacherProfile.objects.select_related("user", "branch", "department")

    def for_user(self, user) -> TeacherProfile | None:
        return self.get_queryset().filter(user=user).first()
