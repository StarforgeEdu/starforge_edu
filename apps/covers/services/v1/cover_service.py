"""CoverService — the layered facade over the cover-request domain functions.

Read scoping is delegated to the repository; the request lifecycle (create / assign /
open-pool / claim / cancel / reject) routes through the transactional domain functions
in ``apps.covers.services`` so the select-for-update lock + lesson reassignment stay in
one place. FK inputs (lesson, cover teacher, claimer) are resolved here → clean 400s.
"""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.covers.dto.cover_dto import CreateCoverDTO
from apps.covers.interfaces.repositories import ICoverRepository
from apps.covers.interfaces.services import ICoverService
from apps.covers.models import CoverRequest
from core.exceptions import PermissionException, ValidationException


class CoverService(ICoverService):
    def __init__(self, covers: ICoverRepository) -> None:
        self._covers = covers

    def scoped_list(
        self,
        *,
        user,
        is_unscoped: bool,
        is_manager: bool,
        manager_branch_ids: set[int],
        teacher_branch_ids: set[int],
    ) -> QuerySet[CoverRequest]:
        return self._covers.scoped(
            user=user,
            is_unscoped=is_unscoped,
            is_manager=is_manager,
            manager_branch_ids=manager_branch_ids,
            teacher_branch_ids=teacher_branch_ids,
        )

    def get_visible(
        self,
        *,
        user,
        is_unscoped: bool,
        is_manager: bool,
        manager_branch_ids: set[int],
        teacher_branch_ids: set[int],
        pk: int,
    ) -> CoverRequest | None:
        return self._covers.get_scoped(
            user=user,
            is_unscoped=is_unscoped,
            is_manager=is_manager,
            manager_branch_ids=manager_branch_ids,
            teacher_branch_ids=teacher_branch_ids,
            pk=pk,
        )

    def create(
        self,
        data: CreateCoverDTO,
        *,
        requester,
        is_unscoped: bool,
        branch_ids: set[int],
    ) -> CoverRequest:
        from apps.covers.services import create_cover_request

        lesson = self._resolve_lesson(data.lesson_id)
        if not is_unscoped and lesson.cohort.branch_id not in branch_ids:
            raise PermissionException(
                _("You can only request cover in your own branch."), code="branch_out_of_scope"
            )
        return create_cover_request(lesson=lesson, requester=requester, reason=data.reason)

    def assign(self, *, cover_id: int, cover_teacher_id: int, actor) -> CoverRequest:
        from apps.covers.services import assign_cover

        return assign_cover(
            cover_id=cover_id, cover_teacher=self._resolve_teacher(cover_teacher_id), actor=actor
        )

    def open_pool(self, *, cover_id: int, actor) -> CoverRequest:
        from apps.covers.services import open_to_pool

        return open_to_pool(cover_id=cover_id, actor=actor)

    def claim(self, *, cover_id: int, claimer_user, actor) -> CoverRequest:
        from apps.covers.services import claim_cover

        return claim_cover(
            cover_id=cover_id, claimer_teacher=self._resolve_own_teacher(claimer_user), actor=actor
        )

    def cancel(self, *, cover_id: int, actor) -> CoverRequest:
        from apps.covers.services import cancel_cover

        return cancel_cover(cover_id=cover_id, actor=actor)

    def reject(self, *, cover_id: int, actor) -> CoverRequest:
        from apps.covers.services import reject_cover

        return reject_cover(cover_id=cover_id, actor=actor)

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _resolve_lesson(lesson_id: int):
        from apps.schedule.models import Lesson

        # Mirrors the old CreateCoverSerializer queryset (only a SCHEDULED lesson may be
        # covered) -> a missing/non-schedulable lesson is a 400 field error, not a 500.
        lesson = Lesson.objects.filter(pk=lesson_id, status=Lesson.Status.SCHEDULED).first()
        if lesson is None:
            raise ValidationException(
                _("Invalid lesson."),
                code="validation_error",
                fields={"lesson": ["Not a schedulable lesson."]},
            )
        return lesson

    @staticmethod
    def _resolve_teacher(teacher_id: int):
        from apps.teachers.models import TeacherProfile

        teacher = TeacherProfile.objects.select_related("user").filter(pk=teacher_id).first()
        # Liveness: an offboarded teacher's login is disabled (User.is_active=False)
        # but the OneToOne TeacherProfile row survives. Assigning them a cover would
        # reassign a live lesson to someone who can't log in / take attendance / be
        # re-covered by the pool — the pool path already filters active staff only, so
        # match that intent on the explicit-assign path. Treat inactive as not-found.
        if teacher is None or not getattr(teacher.user, "is_active", False):
            raise ValidationException(  # mirrors the old AssignCoverSerializer PK field -> 400
                _("Invalid cover teacher."),
                code="validation_error",
                fields={"cover_teacher": ["Not an active teacher."]},
            )
        return teacher

    @staticmethod
    def _resolve_own_teacher(user):
        from apps.teachers.models import TeacherProfile

        teacher = TeacherProfile.objects.filter(user=user).first()
        if teacher is None:  # a cover:write holder without a teacher profile can't claim
            raise ValidationException(_("You are not a teacher."), code="not_a_teacher")
        return teacher
