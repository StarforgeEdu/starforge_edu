"""ORM-backed achievement-grant repository (student/parent wall + staff roster)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.achievements.interfaces.repositories import IAchievementGrantRepository
from apps.achievements.models import Achievement, AchievementGrant
from core.repositories import BaseRepository


class AchievementGrantRepository(BaseRepository[AchievementGrant], IAchievementGrantRepository):
    model = AchievementGrant

    def get_queryset(self) -> QuerySet[AchievementGrant]:
        return AchievementGrant.objects.select_related("achievement", "student", "granted_by")

    def wall_for(self, user) -> QuerySet[AchievementGrant]:
        from apps.students.models import StudentProfile
        from apps.students.selectors import student_profile_for

        # A student sees their own wall; a parent sees their guardian-linked children's.
        student = student_profile_for(user)
        if student is not None:
            student_ids: list[int] = [student.pk]
        else:
            student_ids = list(
                StudentProfile.objects.filter(guardians__parent__user=user).values_list("pk", flat=True)
            )
        return self.get_queryset().filter(student_id__in=student_ids).order_by("-granted_at")

    def grants_of(self, achievement: Achievement) -> QuerySet[AchievementGrant]:
        # achievement_grant_to_dict dereferences g.achievement (a full object, for
        # achievement_detail); without select_related("achievement") that is one extra
        # SELECT per grant row (N+1) — a school-wide achievement granted to thousands
        # renders 1 + page_size queries. student/granted_by are read only as ids.
        return achievement.grants.select_related("achievement", "student", "granted_by")
