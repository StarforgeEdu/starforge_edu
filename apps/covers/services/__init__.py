"""Cover-request services (F18-1).

Domain functions live here (imported by the layered service in ``services/v1``). They
hold the transactional core of the cover flow — the select-for-update lock, the lesson
reassignment + time-overlap handling, and the notification fan-out.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.covers.models import CoverRequest
from core.exceptions import (
    ConflictException,
    NotFoundException,
    PermissionException,
    UnprocessableEntity,
    ValidationException,
)


def _notify(event_type: str, recipient_id, cover: CoverRequest) -> None:
    if not recipient_id:
        return
    from apps.notifications.services import dispatch

    dispatch(
        event_type=event_type,
        recipient_id=recipient_id,
        context={"cover_id": cover.pk, "lesson_id": cover.lesson_id},
        dedupe_key=f"{event_type}:{cover.pk}:{recipient_id}",
    )


@transaction.atomic
def create_cover_request(*, lesson, requester, reason: str = "") -> CoverRequest:
    if lesson.teacher.user_id != requester.id:
        raise PermissionException(
            _("Only the lesson's own teacher can request cover for it."), code="not_lesson_teacher"
        )
    try:
        with transaction.atomic():
            cover = CoverRequest.objects.create(
                lesson=lesson,
                requester=requester,
                reason=reason,
                branch=lesson.cohort.branch,
            )
    except IntegrityError as exc:
        # Only the live-uniqueness constraint maps to a clean 409; anything else is
        # a genuine error and must surface, not be mislabelled as a duplicate.
        if "one_live_cover_per_lesson" not in str(exc):
            raise
        raise ConflictException(
            _("This lesson already has an open cover request."), code="cover_already_requested"
        ) from None
    # Tell the branch's managers a cover is needed.
    for uid in _recipients_with_perm(cover, "cover:approve"):
        _notify("cover.requested", uid, cover)
    return cover


def _recipients_with_perm(cover: CoverRequest, perm: str) -> list[int]:
    """Active users in the cover's branch whose role grants `perm` (the notification
    audience for that permission)."""
    from apps.users.models import RoleMembership
    from core.permissions import roles_with_permission

    qs = RoleMembership.objects.filter(role__in=roles_with_permission(perm), revoked_at__isnull=True)
    if cover.branch_id:
        qs = qs.filter(branch_id=cover.branch_id)
    return list(qs.values_list("user_id", flat=True).distinct())


def _pool_teacher_ids(cover: CoverRequest, *, exclude: set[int]) -> list[int]:
    """The claimable-teacher pool for `cover` (F18-2): users who hold cover:write AND have
    a teacher profile in the cover's branch — only they can actually claim (the claim flow
    requires a teacher profile, and `_approve` rejects an out-of-branch cover teacher).
    `exclude` drops the requester being covered and the manager who opened the pool."""
    from apps.teachers.models import TeacherProfile

    candidates = set(_recipients_with_perm(cover, "cover:write")) - exclude
    if not candidates:
        return []
    teachers = TeacherProfile.objects.filter(user_id__in=candidates)
    if cover.branch_id:
        teachers = teachers.filter(branch_id=cover.branch_id)
    return list(teachers.values_list("user_id", flat=True).distinct())


def _locked(cover_id: int) -> CoverRequest:
    cover = CoverRequest.objects.select_for_update().filter(pk=cover_id).first()
    if cover is None:
        raise NotFoundException(_("Cover request not found."), code="not_found")
    return cover


def _reassign_lesson(lesson, cover_teacher) -> None:
    """Swap the lesson's teacher to the cover teacher — the cover actually takes
    effect. A teacher can't cover their own lesson, nor one that clashes with a
    lesson they already teach (the DB exclusion constraint -> clean 409)."""
    from apps.schedule.models import Lesson

    # The lesson may have left SCHEDULED (cancelled/completed) between request and
    # approval — re-check under the cover lock so we never rewrite history or a
    # lesson the time-overlap constraint no longer protects. `lesson` is loaded
    # fresh via the locked cover, so its status is current.
    if lesson.status != Lesson.Status.SCHEDULED:
        raise UnprocessableEntity(
            _("This lesson can no longer be covered."), code="cover_lesson_not_schedulable"
        )
    if cover_teacher.id == lesson.teacher_id:
        raise ValidationException(_("A teacher cannot cover their own lesson."), code="cant_cover_self")
    lesson.teacher = cover_teacher
    try:
        with transaction.atomic():
            lesson.save(update_fields=["teacher", "updated_at"])
    except IntegrityError:
        raise ConflictException(
            _("The cover teacher already has a lesson at that time."), code="cover_conflict"
        ) from None


def _approve(cover: CoverRequest, *, cover_teacher, actor) -> CoverRequest:
    # A manager may only act on their own branch's requests (get_queryset), and the
    # cover teacher must belong to that same branch — the cover_teacher PK is
    # otherwise unscoped, so without this a branch-A manager could pull a branch-B
    # teacher across the boundary onto a branch-A lesson.
    if cover.branch_id and cover_teacher.branch_id != cover.branch_id:
        raise PermissionException(
            _("The cover teacher must belong to the lesson's branch."),
            code="cover_teacher_out_of_branch",
        )
    _reassign_lesson(cover.lesson, cover_teacher)
    cover.cover_teacher = cover_teacher
    cover.status = CoverRequest.Status.APPROVED
    cover.decided_by = actor
    cover.decided_at = timezone.now()
    cover.save(update_fields=["cover_teacher", "status", "decided_by", "decided_at", "updated_at"])
    _notify("cover.approved", cover.requester_id, cover)
    return cover


@transaction.atomic
def assign_cover(*, cover_id: int, cover_teacher, actor=None) -> CoverRequest:
    """A manager assigns a specific cover teacher (and approves)."""
    cover = _locked(cover_id)
    if cover.status != CoverRequest.Status.OPEN:
        raise UnprocessableEntity(_("Only an open cover request can be assigned."), code="cover_not_open")
    return _approve(cover, cover_teacher=cover_teacher, actor=actor)


@transaction.atomic
def open_to_pool(*, cover_id: int, actor=None) -> CoverRequest:
    """A manager opens the request to the branch's teacher pool to claim, and the pool is
    notified in realtime (F18-2) — so a teacher learns a lesson is up for grabs and can
    claim it, rather than having to poll the board. Reuses the notification fan-out (and
    its WebSocket push); no bespoke chat channel."""
    cover = _locked(cover_id)
    if cover.status != CoverRequest.Status.OPEN:
        raise UnprocessableEntity(
            _("Only an open cover request can be opened to the pool."), code="cover_not_open"
        )
    cover.pool = True
    cover.save(update_fields=["pool", "updated_at"])
    exclude = {i for i in (cover.requester_id, getattr(actor, "id", None)) if i is not None}
    for uid in _pool_teacher_ids(cover, exclude=exclude):
        _notify("cover.pool_opened", uid, cover)
    return cover


@transaction.atomic
def claim_cover(*, cover_id: int, claimer_teacher, actor=None) -> CoverRequest:
    """A teacher claims a pooled cover request (and it is approved to them)."""
    cover = _locked(cover_id)
    if cover.status != CoverRequest.Status.OPEN or not cover.pool:
        raise UnprocessableEntity(
            _("This cover request is not open to the pool."), code="cover_not_claimable"
        )
    return _approve(cover, cover_teacher=claimer_teacher, actor=actor)


@transaction.atomic
def cancel_cover(*, cover_id: int, actor=None) -> CoverRequest:
    cover = _locked(cover_id)
    if cover.status != CoverRequest.Status.OPEN:
        raise UnprocessableEntity(_("Only an open cover request can be cancelled."), code="cover_not_open")
    cover.status = CoverRequest.Status.CANCELLED
    cover.decided_by = actor
    cover.decided_at = timezone.now()
    cover.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    return cover


@transaction.atomic
def reject_cover(*, cover_id: int, actor=None) -> CoverRequest:
    cover = _locked(cover_id)
    if cover.status != CoverRequest.Status.OPEN:
        raise UnprocessableEntity(_("Only an open cover request can be rejected."), code="cover_not_open")
    cover.status = CoverRequest.Status.REJECTED
    cover.decided_by = actor
    cover.decided_at = timezone.now()
    cover.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    _notify("cover.rejected", cover.requester_id, cover)
    return cover
