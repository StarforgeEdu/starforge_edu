"""AchievementService — the layered facade over the achievement domain functions.

Read scoping is delegated to the repository; writes route through the transactional
domain functions in ``apps.achievements.services`` (create/decide/grant), so the
select-for-update decision path and the grant guards stay in one place.
"""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.achievements.dto.achievement_dto import CreateAchievementDTO, GrantAchievementDTO
from apps.achievements.interfaces.repositories import (
    IAchievementGrantRepository,
    IAchievementRepository,
)
from apps.achievements.interfaces.services import IAchievementService
from apps.achievements.models import Achievement, AchievementGrant
from core.exceptions import ValidationException


class AchievementService(IAchievementService):
    def __init__(self, achievements: IAchievementRepository, grants: IAchievementGrantRepository) -> None:
        self._achievements = achievements
        self._grants = grants

    def scoped_list(
        self, *, user, is_unscoped: bool, can_write: bool, can_approve: bool, branch_ids: set[int]
    ) -> QuerySet[Achievement]:
        return self._achievements.scoped(
            user=user,
            is_unscoped=is_unscoped,
            can_write=can_write,
            can_approve=can_approve,
            branch_ids=branch_ids,
        )

    def get_visible(
        self, *, user, is_unscoped: bool, can_write: bool, can_approve: bool, branch_ids: set[int], pk: int
    ) -> Achievement | None:
        return self._achievements.get_scoped(
            user=user,
            is_unscoped=is_unscoped,
            can_write=can_write,
            can_approve=can_approve,
            branch_ids=branch_ids,
            pk=pk,
        )

    def create(
        self, data: CreateAchievementDTO, *, creator, can_approve: bool, is_scoped: bool, branch_ids: set[int]
    ) -> Achievement:
        from apps.achievements.services import create_achievement

        return create_achievement(
            creator=creator,
            can_approve=can_approve,
            is_scoped=is_scoped,
            creator_branch_ids=branch_ids,
            name=data.name,
            scope=data.scope,
            description=data.description,
            emoji=data.emoji,
            cohort=self._resolve_cohort(data.cohort_id),
        )

    def decide(self, *, achievement_id: int, approve: bool, actor) -> Achievement:
        from apps.achievements.services import decide_achievement

        return decide_achievement(achievement_id=achievement_id, approve=approve, actor=actor)

    def grant(
        self, achievement: Achievement, data: GrantAchievementDTO, *, granted_by, student=None
    ) -> AchievementGrant:
        from apps.achievements.services import grant_achievement

        # The view resolves + branch-scope-checks the recipient (object-level IDOR
        # guard); accept that instance. Fall back to resolving by id for any internal
        # caller that passes only the dto.
        return grant_achievement(
            achievement=achievement,
            student=student if student is not None else self._resolve_student(data.student_id),
            granted_by=granted_by,
            note=data.note,
        )

    def resolve_student(self, student_id: int):
        """Public resolver for the view's object-level scope check (returns the
        StudentProfile or None; the view decides 400-invalid vs 403-out-of-branch)."""
        from apps.students.models import StudentProfile

        return StudentProfile.objects.filter(pk=student_id).first()

    def wall_for(self, user) -> QuerySet[AchievementGrant]:
        return self._grants.wall_for(user)

    def grants_of(self, achievement: Achievement) -> QuerySet[AchievementGrant]:
        return self._grants.grants_of(achievement)

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _resolve_cohort(cohort_id: int | None):
        if cohort_id is None:
            return None
        from apps.cohorts.models import Cohort

        cohort = Cohort.objects.filter(pk=cohort_id).first()
        if cohort is None:  # mirrors the old PrimaryKeyRelatedField -> 400 field error
            raise ValidationException(
                _("Invalid cohort."), code="validation_error", fields={"cohort": ["Not found."]}
            )
        return cohort

    @staticmethod
    def _resolve_student(student_id: int):
        from apps.students.models import StudentProfile

        student = StudentProfile.objects.filter(pk=student_id).first()
        if student is None:  # mirrors the old PrimaryKeyRelatedField -> 400 field error
            raise ValidationException(
                _("Invalid student."), code="validation_error", fields={"student": ["Not found."]}
            )
        return student
