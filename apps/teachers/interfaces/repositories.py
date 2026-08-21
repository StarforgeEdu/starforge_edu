"""Teacher repository port."""

from __future__ import annotations

from apps.teachers.models import TeacherProfile
from core.interfaces import IBaseRepository


class ITeacherRepository(IBaseRepository[TeacherProfile]):
    def for_user(self, user) -> TeacherProfile | None:
        """The signed-in user's own teacher profile (dashboard), or None."""
        raise NotImplementedError
