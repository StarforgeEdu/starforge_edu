"""Rule book services: create/update (auto version-bump on body change) +
acknowledge."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.compliance.models import Penalty, Rule, RuleAcknowledgment
from apps.org.selectors import get_center_settings
from core.exceptions import NotFoundException, UnprocessableEntity

logger = logging.getLogger("starforge.compliance")


@transaction.atomic
def update_rule_body(*, rule: Rule, body: str | None = None, title: str | None = None, **fields) -> Rule:
    """Apply edits; bump the version (forcing re-acknowledgment) only when the body
    actually changes."""
    if body is not None and body != rule.body:
        rule.body = body
        rule.version += 1
    if title is not None:
        rule.title = title
    for key, value in fields.items():
        setattr(rule, key, value)
    rule.save()
    return rule


@transaction.atomic
def acknowledge(*, rule: Rule, user) -> RuleAcknowledgment:
    """Record that `user` accepted the CURRENT version of `rule`. Idempotent."""
    ack, _created = RuleAcknowledgment.objects.get_or_create(rule=rule, user=user, version=rule.version)
    return ack


def _active_points(student_id: int) -> int:
    """Sum of a student's ACTIVE (un-waived) penalty points."""
    total = Penalty.objects.filter(student_id=student_id, status=Penalty.Status.ACTIVE).aggregate(
        total=Sum("points")
    )["total"]
    return total or 0


def _escalation_manager_ids(branch_id: int | None) -> list[int]:
    """Active users who handle penalties (penalty:waive) in the student's branch — the
    people who should know a student is accumulating demerits. Scoped to the branch so
    one branch's pattern doesn't ping another branch's managers."""
    from core.permissions import role_memberships_with_permission

    qs = role_memberships_with_permission("penalty:waive")
    if branch_id is not None:
        qs = qs.filter(branch_id=branch_id)
    return list(qs.values_list("user_id", flat=True).distinct())


def _notify_escalation(*, penalty: Penalty, total_points: int, threshold: int) -> None:
    """Best-effort: tell the branch's managers a student crossed the penalty threshold.
    The ENTIRE body is guarded — this runs from transaction.on_commit AFTER the penalty
    has committed, so neither the recipient lookup (DB queries) nor a dispatch failure
    may propagate out and 500 a request whose penalty is already saved. A per-penalty
    dedupe_key makes a retry idempotent."""
    try:
        from apps.notifications.services import dispatch

        context = {
            "student_id": penalty.student_id,
            "penalty_id": penalty.pk,
            "total_points": total_points,
            "threshold": threshold,
        }
        for uid in _escalation_manager_ids(penalty.branch_id):
            try:
                dispatch(
                    event_type="penalty.escalated",
                    recipient_id=uid,
                    context=context,
                    dedupe_key=f"penalty_escalation:{penalty.pk}:{uid}",
                )
            except Exception:
                # one bad recipient must not skip the others
                logger.exception("penalty escalation dispatch failed (penalty=%s, user=%s)", penalty.pk, uid)
    except Exception:
        # recipient resolution / import failure must never break the committed penalty
        logger.exception("penalty escalation notify failed (penalty=%s)", penalty.pk)


@transaction.atomic
def issue_penalty(*, student, points: int, reason: str, issued_by, rule=None) -> Penalty:
    """Issue a demerit against a student. The branch is taken from the student, so the
    penalty is always attributable to where the student belongs (no branch guessing).

    F24-1: if this penalty pushes the student's total ACTIVE points across the center's
    `penalty_escalation_threshold` (an UPWARD crossing — fires once at the boundary, not
    on every later penalty), flag it + notify branch managers so a pattern of breaches
    surfaces to management automatically (accountability DNA)."""
    threshold = get_center_settings().penalty_escalation_threshold or 0
    before = 0
    if threshold:
        # Lock the student row so concurrent issuance for the SAME student serializes;
        # otherwise two transactions read the same pre-insert active total (READ
        # COMMITTED) and can both MISS a combined crossing — permanently, since every
        # later penalty then sees before >= threshold — or both escalate (double-alert).
        from apps.students.models import StudentProfile

        StudentProfile.objects.select_for_update().filter(pk=student.id).first()
        before = _active_points(student.id)
    after = before + points
    escalate = bool(threshold) and before < threshold <= after
    penalty = Penalty.objects.create(
        student=student,
        points=points,
        reason=reason,
        branch=student.branch,
        issued_by=issued_by,
        rule=rule,
        escalated=escalate,
    )
    if escalate:
        # Notify after commit so a rolled-back penalty never sends a phantom alert.
        transaction.on_commit(
            lambda: _notify_escalation(penalty=penalty, total_points=after, threshold=threshold)
        )
    return penalty


@transaction.atomic
def issue_staff_penalty(*, staff, points: int, reason: str, issued_by, branch, rule=None) -> Penalty:
    """Issue a disciplinary penalty against a STAFF member (F24-1). Manager-gated by
    penalty:staff at the view. Guards: a person may NOT penalise themselves (segregation
    of duties), and the subject must be an active staff member — never a student/parent
    (mirrors the loan/reward recipient guard). The branch is the issuing manager's branch
    context (validated at the view), which scopes who may later see/waive it. Staff
    penalties carry no point-threshold escalation (that is a student-intake signal)."""
    from apps.access.models import AccountType
    from core.permissions import role_memberships_for_account_kinds

    if getattr(staff, "id", None) is not None and staff.id == getattr(issued_by, "id", None):
        raise UnprocessableEntity(_("You cannot penalise yourself."), code="self_penalty")
    # The subject must be an active staff member OF THE PENALTY'S BRANCH — symmetric with
    # the student path (which forces student.branch into the manager's scope). Without the
    # branch filter a manager could file discipline against staff from another branch,
    # hidden from that staff member's real branch managers.
    is_staff = (
        role_memberships_for_account_kinds((AccountType.AccountKind.STAFF, AccountType.AccountKind.TEACHER))
        .filter(user=staff, branch=branch)
        .exists()
    )
    if not is_staff:
        raise UnprocessableEntity(
            _("A staff penalty's subject must be an active staff member of that branch."),
            code="not_staff",
        )
    return Penalty.objects.create(
        staff=staff, points=points, reason=reason, branch=branch, issued_by=issued_by, rule=rule
    )


@transaction.atomic
def waive_penalty(*, penalty_id: int, actor, reason: str = "") -> Penalty:
    """Reverse an active penalty (a manager corrects a mistake / accepts an appeal).
    Locked + active-only, so a penalty can't be double-waived. Issuing (penalty:write)
    and waiving (penalty:waive) are separate permissions — the teacher who issued a
    demerit can't quietly undo it; a manager must."""
    penalty = Penalty.objects.select_for_update().filter(pk=penalty_id).first()
    if penalty is None:
        raise NotFoundException(_("Penalty not found."), code="penalty_not_found")
    if penalty.status != Penalty.Status.ACTIVE:
        raise UnprocessableEntity(_("Only an active penalty can be waived."), code="penalty_not_active")
    penalty.status = Penalty.Status.WAIVED
    penalty.waived_by = actor
    penalty.waived_at = timezone.now()
    penalty.waive_reason = reason
    penalty.save(update_fields=["status", "waived_by", "waived_at", "waive_reason"])
    return penalty
