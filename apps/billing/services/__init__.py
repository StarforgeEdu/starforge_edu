"""Billing write-side services (PUBLIC schema).

Plan-limit enforcement, the trial/active/past_due/suspended state machine,
dunning fan-out, and platform subscription payment intake.

Cross-lane lazy imports: dunning calls `apps.notifications.services.dispatch`
and `apps.audit.services.audit_log` inside `schema_context` — both imported
lazily inside the function so this module loads even while sibling lanes are
mid-build. Platform checkout reuses the `infrastructure/payments` mock clients
with OWNER credentials.

Email exception (D3-E-9): billing is platform infrastructure, not a domain app,
so it MAY send a direct email to `Center.contact_email`. Domain apps must not.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from functools import partial

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_tenants.utils import schema_context

from apps.billing.models import Plan, Subscription
from apps.billing.selectors import subscription_cache_key
from core.exceptions import NotFoundException, StarforgeError, ValidationException

logger = logging.getLogger("starforge.billing")

TRIAL_DEFAULT_DAYS = 14  # fallback period when Center.trial_ends_at is unset
PLATFORM_EXTENSION_DAYS = 30  # mock payment extends the period by this much
_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")


class PlanLimitExceeded(StarforgeError):
    """402 — enrolling past the plan's max_students. Surfaced through TD-18."""

    code = "plan_limit_exceeded"
    status_code = 402
    default_detail = _("Your plan's student limit has been reached.")


def _invalidate_subscription_cache(schema_name: str) -> None:
    cache.delete(subscription_cache_key(schema_name))


# ---------------------------------------------------------------------------
# Metered AI overage billing (F9-2)
# ---------------------------------------------------------------------------
def meter_ai_overage(*, center_id: int, on=None):
    """Record the AI-overage charge for a Center's current billing month (F9-2).

    AI generation beyond the plan's ``ai_tokens_month`` allowance is billed per use.
    Cross-schema like the nightly meter: reads the Subscription/Plan on the PUBLIC
    schema, the month's AI token + provider-cost totals inside the Center's tenant
    schema, then upserts the public ``AiUsageCharge`` row. Idempotent per (center,
    period) — re-running re-computes the month-to-date charge in place (never
    duplicated). Returns the charge, or None when the Center has no subscription
    (nothing to bill against) or is inactive."""
    from django_tenants.utils import get_public_schema_name

    from apps.ai.selectors import cost_consumed, tokens_consumed
    from apps.billing.models import AiUsageCharge
    from apps.tenancy.models import Center

    # The billing month is the center's LOCAL month. Derive BOTH the charge bucket
    # (period) AND the usage window from this single local date — never mix a UTC date
    # for the period with a local-month token window, or at a month boundary (local
    # 00:00-05:00 == prior-day UTC) the period would resolve to the previous month while
    # the usage is the new month, overwriting the prior month's finalized charge.
    on = on or timezone.localdate()
    period = on.replace(day=1)

    with schema_context(get_public_schema_name()):
        center = Center.objects.filter(pk=center_id, is_active=True).first()
        sub = (
            Subscription.objects.select_related("plan").filter(center_id=center_id).first()
            if center is not None
            else None
        )
    if center is None or sub is None:
        return None

    allowance = sub.plan.ai_tokens_month
    rate = sub.plan.ai_overage_price_per_1k_uzs or _ZERO

    with schema_context(center.schema_name):
        used_tokens = tokens_consumed(period, on)  # [first-of-month, on] — same date source
        cost_microusd = cost_consumed(period, on)

    overage = max(0, used_tokens - allowance)
    amount = (Decimal(overage) * rate / Decimal(1000)).quantize(_CENT) if overage > 0 else _ZERO

    with schema_context(get_public_schema_name()):
        charge, _created = AiUsageCharge.objects.update_or_create(
            center_id=center_id,
            period=period,
            defaults={
                "included_tokens": allowance,
                "used_tokens": used_tokens,
                "overage_tokens": overage,
                "rate_per_1k_uzs": rate,
                "amount_uzs": amount,
                "cost_microusd": cost_microusd,
            },
        )
    return charge


# ---------------------------------------------------------------------------
# Auto-subscription (D3-E-3)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_trial_subscription(*, center) -> Subscription:
    """Create a trialing Subscription for a freshly provisioned Center.

    Idempotent: re-firing the post_save receiver returns the existing row.
    Trial dates come from `Center.trial_ends_at` (or now + 14d fallback).
    """
    existing = Subscription.objects.filter(center=center).first()
    if existing is not None:
        return existing
    plan = _default_plan()
    now = timezone.now()
    period_end = center.trial_ends_at or (now + timedelta(days=TRIAL_DEFAULT_DAYS))
    sub = Subscription.objects.create(
        center=center,
        plan=plan,
        status=Subscription.Status.TRIALING,
        current_period_start=now,
        current_period_end=period_end,
    )
    # Invalidate AFTER commit: clearing inside the tx lets a concurrent request
    # re-cache the pre-change status from the uncommitted row (paywall re-poison).
    transaction.on_commit(partial(_invalidate_subscription_cache, center.schema_name))
    return sub


def _default_plan() -> Plan:
    """The cheapest active plan (starter), or any active plan as a fallback."""
    plan = Plan.objects.filter(is_active=True).order_by("price_uzs").first()
    if plan is None:
        raise StarforgeError(_("No active subscription plan is configured."), code="no_plan_configured")
    return plan


# ---------------------------------------------------------------------------
# Plan-limit enforcement (D3-E-7)
# ---------------------------------------------------------------------------
def enforce_student_limit() -> None:
    """Raise `PlanLimitExceeded` (402) if the current tenant is at its student cap.

    Reads `connection.tenant` (the Center, set by TenantMainMiddleware), looks up
    its subscription/plan in the public schema, and counts active students in the
    tenant schema. Called from the ONE enrollment site in apps/students/services.py
    (see integration_needed). No subscription / no cap configured → no-op (never
    blocks a center the platform has not metered yet).

    BEST-EFFORT (not strict): this is a check-then-act with no seat lock, so two
    concurrent enrollments at (cap - 1) could both pass and exceed the cap by a
    small margin. The cap is a soft commercial guardrail (the nightly meter
    reconciles usage), so this is acceptable; strict enforcement would require
    holding a per-tenant lock across the count AND the enrollment commit.
    """
    center = getattr(connection, "tenant", None)
    if center is None:
        return  # public schema or unresolved tenant: nothing to enforce
    schema_name = getattr(connection, "schema_name", None)
    if not schema_name or schema_name == _public_schema_name():
        return
    # Resolve by schema_name, not center.pk: inside schema_context (Celery, tests)
    # connection.tenant is a FakeTenant that has schema_name but no pk.
    with schema_context(_public_schema_name()):
        sub = Subscription.objects.select_related("plan").filter(center__schema_name=schema_name).first()
    if sub is None:
        return
    max_students = sub.plan.max_students
    if not max_students:  # 0 / None = unlimited
        return
    with schema_context(schema_name):
        active = _active_student_count()
    if active >= max_students:
        raise PlanLimitExceeded(
            _("Your plan allows up to %(max)s students; upgrade to enroll more.") % {"max": max_students}
        )


def _active_student_count() -> int:
    """Seat-consuming students: enrolled OR active (not leads/graduated/withdrawn).

    Must match `celery_tasks.billing_tasks._students_count` so the metered count
    and the enforced cap agree.
    """
    from apps.students.models import StudentProfile

    return StudentProfile.objects.filter(
        status__in=(StudentProfile.Status.ENROLLED, StudentProfile.Status.ACTIVE)
    ).count()


def _public_schema_name() -> str:
    from django_tenants.utils import get_public_schema_name

    return get_public_schema_name()


# ---------------------------------------------------------------------------
# Metering + state flips (D3-E-5). Bodies are called per-Center from the task.
# ---------------------------------------------------------------------------
def evaluate_subscription_state(*, subscription: Subscription, now=None) -> str | None:
    """Pure-ish state evaluator. Returns the NEW status if a flip is warranted,
    else None. Does not persist — `apply_state_flip` does that + side effects.

    - trialing past period_end + BILLING_TRIAL_GRACE_DAYS → suspended
    - active past period_end → past_due
    - past_due longer than BILLING_DUNNING_DAYS (after period_end) → suspended
    """
    now = now or timezone.now()
    grace_days = int(getattr(settings, "BILLING_TRIAL_GRACE_DAYS", 3))
    dunning_days = int(getattr(settings, "BILLING_DUNNING_DAYS", 7))
    status = subscription.status
    end = subscription.current_period_end

    if status == Subscription.Status.TRIALING and now > end + timedelta(days=grace_days):
        return Subscription.Status.SUSPENDED
    if status == Subscription.Status.ACTIVE and now > end:
        return Subscription.Status.PAST_DUE
    if status == Subscription.Status.PAST_DUE and now > end + timedelta(days=dunning_days):
        return Subscription.Status.SUSPENDED
    return None


@transaction.atomic
def apply_state_flip(*, subscription: Subscription, new_status: str) -> Subscription:
    """Persist a status change and run the dunning side effects for past_due /
    suspended. Cache-invalidates the tenant's gate lookup."""
    old_status = subscription.status
    if old_status == new_status:
        return subscription
    subscription.status = new_status
    subscription.save(update_fields=["status", "updated_at"])
    transaction.on_commit(partial(_invalidate_subscription_cache, subscription.center.schema_name))

    if new_status in (Subscription.Status.PAST_DUE, Subscription.Status.SUSPENDED):
        transaction.on_commit(lambda: _run_dunning(center_id=subscription.center_id, status=new_status))
    _audit_subscription_change(
        center=subscription.center,
        old_status=old_status,
        new_status=new_status,
    )
    return subscription


# ---------------------------------------------------------------------------
# Dunning (D3-E-9): notifications.dispatch for directors + direct email + audit
# ---------------------------------------------------------------------------
def _run_dunning(*, center_id: int, status: str) -> None:
    """Notify directors (in-app/email/SMS via the notifications pipeline) and
    email the Center's contact. Runs inside the tenant schema for the dispatch
    (Notification rows are tenant-scoped); the email is a platform-level send."""
    from apps.tenancy.models import Center

    center = Center.objects.filter(pk=center_id).first()
    if center is None:
        return
    event_type = (
        "billing.subscription_suspended"
        if status == Subscription.Status.SUSPENDED
        else "billing.subscription_past_due"
    )
    today = timezone.localdate().isoformat()
    with schema_context(center.schema_name):
        _dispatch_to_directors(center=center, event_type=event_type, status=status, date=today)
    _email_center_contact(center=center, status=status)


def _dispatch_to_directors(*, center, event_type: str, status: str, date: str) -> None:
    from core.permissions import Role

    try:
        from apps.notifications.services import dispatch
    except Exception:  # notifications lane not merged yet — degrade, never crash
        logger.warning("notifications.dispatch unavailable; skipping dunning dispatch")
        return
    director_ids = _director_user_ids()
    for user_id in director_ids:
        dedupe_key = f"billing:{center.pk}:{status}:{date}"
        try:
            dispatch(
                event_type=event_type,
                recipient_id=user_id,
                context={
                    "center_name": center.name,
                    "status": status,
                    "contact_email": center.contact_email,
                },
                dedupe_key=f"{dedupe_key}:{user_id}",
            )
        except Exception:  # one bad recipient must not abort the rest
            logger.exception("billing dunning dispatch failed", extra={"user_id": user_id})
    _ = Role  # role import documents the director resolution path


def _director_user_ids() -> list[int]:
    """User ids holding an active director RoleMembership in the current schema."""
    from apps.users.models import RoleMembership
    from core.permissions import Role

    return list(
        RoleMembership.objects.filter(role=Role.DIRECTOR, revoked_at__isnull=True)
        .values_list("user_id", flat=True)
        .distinct()
    )


def _email_center_contact(*, center, status: str) -> None:
    if not center.contact_email:
        return
    try:
        from infrastructure.email.email_client import send_email
    except Exception:
        return
    if status == Subscription.Status.SUSPENDED:
        subject = str(_("Your Starforge subscription has been suspended"))
        body = str(
            _(
                "Service for %(name)s has been suspended for non-payment. Please settle your balance to restore access."
            )
            % {"name": center.name}
        )
    else:
        subject = str(_("Your Starforge subscription payment is past due"))
        body = str(
            _("The subscription for %(name)s is past due. Please make a payment to avoid suspension.")
            % {"name": center.name}
        )
    try:
        send_email(to=center.contact_email, subject=subject, body=body)
    except Exception:  # email transport failure must not abort the flip
        logger.exception("billing dunning email failed", extra={"center_id": center.pk})


def _audit_subscription_change(*, center, old_status: str, new_status: str) -> None:
    """Write a subscription-change audit row inside the tenant schema (Lane D
    decision: a public-schema Subscription cannot be audited by a tenant
    post_save receiver, so Lane E logs it explicitly here)."""
    try:
        from apps.audit.services import audit_log
    except Exception:  # audit lane not merged yet
        logger.info(
            "subscription %s: %s -> %s (audit lane unavailable)",
            center.pk,
            old_status,
            new_status,
        )
        return
    with schema_context(center.schema_name):
        try:
            audit_log(
                actor=None,
                action="update",
                resource_type="billing.Subscription",
                resource_id=str(center.pk),
                before={"status": old_status},
                after={"status": new_status},
            )
        except Exception:
            logger.exception("subscription audit_log failed", extra={"center_id": center.pk})


# ---------------------------------------------------------------------------
# Platform subscription payment intake (D3-E-8)
# ---------------------------------------------------------------------------
@transaction.atomic
def change_subscription(
    *, center_id: int, plan_code: str | None = None, status: str | None = None
) -> Subscription:
    """Platform-staff PATCH: change plan and/or set status active|suspended."""
    sub = Subscription.objects.select_related("plan", "center").filter(center_id=center_id).first()
    if sub is None:
        raise NotFoundException(_("No subscription for that center."))
    if plan_code is not None:
        plan = Plan.objects.filter(code=plan_code).first()
        if plan is None:
            raise ValidationException(
                _("Unknown plan code."), code="unknown_plan", fields={"plan": [plan_code]}
            )
        sub.plan = plan
    fields = ["plan", "status", "updated_at"]
    if status is not None:
        if status not in (Subscription.Status.ACTIVE, Subscription.Status.SUSPENDED):
            raise ValidationException(
                _("Status may only be set to active or suspended."),
                code="invalid_status",
                fields={"status": [status]},
            )
        old = sub.status
        sub.status = status
        # Reactivating a LAPSED center must also extend the billing period. A center
        # reaches suspended precisely because current_period_end is in the past; if
        # activate only flips the status, the nightly meter re-flips ACTIVE->PAST_DUE
        # (now > current_period_end) on its next run and re-suspends it — making
        # activate a no-op in production. Grant a fresh cycle, the same convention the
        # mock-payment path uses (PLATFORM_EXTENSION_DAYS).
        if status == Subscription.Status.ACTIVE and sub.current_period_end <= timezone.now():
            sub.current_period_end = timezone.now() + timedelta(days=PLATFORM_EXTENSION_DAYS)
            fields.append("current_period_end")
        if old != status:
            _audit_subscription_change(center=sub.center, old_status=old, new_status=status)
    sub.save(update_fields=fields)
    transaction.on_commit(partial(_invalidate_subscription_cache, sub.center.schema_name))
    return sub


@transaction.atomic
def extend_trial_period(*, center_id: int, new_trial_ends_at) -> Subscription | None:
    """Sync a TRIALING subscription's period to a newly-extended trial end.

    create_trial_subscription sets current_period_end == Center.trial_ends_at, but a
    later tenancy.extend_trial only moved trial_ends_at — leaving the subscription's
    stale period_end, so the nightly meter suspended (402) the tenant at the ORIGINAL
    trial end despite the extension. Re-establish the invariant. No-op if there is no
    subscription or it is no longer trialing (a converted/suspended sub isn't a trial)."""
    sub = Subscription.objects.filter(center_id=center_id).first()
    if sub is None or sub.status != Subscription.Status.TRIALING:
        return sub
    if new_trial_ends_at and new_trial_ends_at > sub.current_period_end:
        sub.current_period_end = new_trial_ends_at
        sub.save(update_fields=["current_period_end", "updated_at"])
        transaction.on_commit(partial(_invalidate_subscription_cache, sub.center.schema_name))
    return sub


@transaction.atomic
def process_platform_checkout(*, center_id: int, provider: str = "payme") -> Subscription:
    """Mock-first platform subscription payment.

    Reuses the `infrastructure/payments` mock client with OWNER credentials
    (PLATFORM_* env, default mock). On (mock) success: extend
    `current_period_end` by 30d and set the subscription `active`.
    """
    sub = Subscription.objects.select_related("plan", "center").filter(center_id=center_id).first()
    if sub is None:
        raise NotFoundException(_("No subscription for that center."))

    result = _charge_platform(provider=provider, amount_uzs=sub.plan.price_uzs, center=sub.center)
    if not result.get("ok"):
        raise ValidationException(_("Platform payment failed."), code="platform_payment_failed")

    now = timezone.now()
    base = max(sub.current_period_end, now)
    sub.current_period_end = base + timedelta(days=PLATFORM_EXTENSION_DAYS)
    old = sub.status
    sub.status = Subscription.Status.ACTIVE
    sub.save(update_fields=["current_period_end", "status", "updated_at"])
    transaction.on_commit(partial(_invalidate_subscription_cache, sub.center.schema_name))
    if old != Subscription.Status.ACTIVE:
        _audit_subscription_change(center=sub.center, old_status=old, new_status=sub.status)
    return sub


def _charge_platform(*, provider: str, amount_uzs: Decimal, center) -> dict:
    """Mock platform charge. Deterministic; flips real when PLATFORM_*_USE_MOCK
    is False AND owner credentials land (TD-2). For Day 3 this is always the mock.
    """
    use_mock = bool(getattr(settings, "PLATFORM_PAYMENTS_USE_MOCK", True))
    if use_mock:
        logger.info(
            "MOCK platform charge provider=%s amount=%s center=%s",
            provider,
            amount_uzs,
            center.pk,
        )
        return {
            "ok": True,
            "mock": True,
            "provider": provider,
            "amount_uzs": str(amount_uzs),
            "txn_id": f"platform-{provider}-{center.pk}",
        }
    # Real path would build the provider client with PLATFORM_* owner creds and
    # confirm the charge; [OWNER:O-3][OWNER:O-4]. Lazy-import inside this branch
    # so the heavy provider client is only touched when actually used.
    raise StarforgeError(
        _("Real platform payments are not configured yet."), code="platform_payments_not_configured"
    )
