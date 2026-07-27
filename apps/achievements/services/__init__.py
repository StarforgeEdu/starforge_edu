"""Achievement services (F15-2).

Domain functions live here (imported by the layered service in ``services/v1``).
They are kept as module-level functions — the transactional core of the feature — so
the select-for-update decision path and the grant guards are exercised directly by
the ``next_meeting_for``-style callers and the service layer alike.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.achievements.models import Achievement, AchievementGrant
from core.exceptions import (
    ConflictException,
    NotFoundException,
    PermissionException,
    UnprocessableEntity,
    ValidationException,
)


@transaction.atomic
def create_achievement(
    *,
    creator,
    can_approve: bool,
    is_scoped: bool,
    creator_branch_ids: set[int],
    name: str,
    scope: str,
    description: str = "",
    emoji: str = "",
    cohort=None,
) -> Achievement:
    """A GROUP achievement is active immediately (a teacher's own class) and its
    branch is DERIVED from the cohort — a scoped (non-director) creator may only make
    one for a cohort in their own branch (no cross-branch write). A GLOBAL one is
    center-wide (branch=None) and active only if the creator may approve (a manager);
    otherwise PENDING until a manager approves — the teacher→manager request flow."""
    if scope == Achievement.Scope.GROUP:
        if cohort is None:
            raise ValidationException(_("A group achievement needs a cohort."), code="cohort_required")
        if is_scoped and cohort.branch_id not in creator_branch_ids:
            raise PermissionException(
                _("You can only create an achievement for a cohort in your own branch."),
                code="cross_branch",
            )
        branch = cohort.branch  # derived, never client-supplied
        status = Achievement.Status.ACTIVE
    else:  # GLOBAL — center-wide, no cohort/branch
        cohort = None
        branch = None
        status = Achievement.Status.ACTIVE if can_approve else Achievement.Status.PENDING
    return Achievement.objects.create(
        name=name,
        description=description,
        emoji=emoji,
        scope=scope,
        cohort=cohort,
        branch=branch,
        status=status,
        created_by=creator,
    )


def _locked(achievement_id: int) -> Achievement:
    achievement = Achievement.objects.select_for_update().filter(pk=achievement_id).first()
    if achievement is None:
        raise NotFoundException(_("Achievement not found."), code="not_found")
    return achievement


@transaction.atomic
def decide_achievement(*, achievement_id: int, approve: bool, actor=None) -> Achievement:
    achievement = _locked(achievement_id)
    if achievement.status != Achievement.Status.PENDING:
        raise UnprocessableEntity(
            _("Only a pending achievement can be decided."), code="achievement_not_pending"
        )
    achievement.status = Achievement.Status.ACTIVE if approve else Achievement.Status.REJECTED
    achievement.decided_by = actor
    achievement.decided_at = timezone.now()
    achievement.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    return achievement


@transaction.atomic
def grant_achievement(
    *, achievement: Achievement, student, granted_by=None, note: str = ""
) -> AchievementGrant:
    if achievement.status != Achievement.Status.ACTIVE:
        raise UnprocessableEntity(
            _("Only an active achievement can be granted."), code="achievement_not_active"
        )
    if achievement.scope == Achievement.Scope.GROUP and student.current_cohort_id != achievement.cohort_id:
        raise UnprocessableEntity(
            _("This achievement belongs to a different group."), code="student_not_in_group"
        )
    try:
        with transaction.atomic():
            return AchievementGrant.objects.create(
                achievement=achievement, student=student, granted_by=granted_by, note=note
            )
    except IntegrityError:
        raise ConflictException(
            _("This student already has this achievement."), code="already_granted"
        ) from None
