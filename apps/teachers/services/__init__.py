"""Teacher write services (TASKS §7) + the F13-1 dynamic payout/salary engine."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.teachers.models import PayoutPolicy, TeacherProfile
from apps.users.services import create_role_user_bridge, ensure_role_membership, prepare_role_identity
from core.exceptions import ConflictException, UnprocessableEntity, ValidationException
from core.permissions import Role
from core.utils import current_schema, stable_hash

_CENT = Decimal("0.01")
_HOUR = Decimal("3600")
_MAX_PAYOUT_PERIOD_DAYS = 366


@transaction.atomic
def create_teacher(
    *,
    branch,
    department=None,
    phone: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    middle_name: str = "",
    birthdate=None,
    gender: str = "",
    hire_date=None,
    subjects: list | None = None,
    qualifications: str = "",
    salary_type: str = TeacherProfile.SalaryType.MONTHLY,
    rate=None,
    is_substitute: bool = False,
    username: str = "",
    account_type=None,
) -> TeacherProfile:
    if department is not None and department.branch_id != branch.id:
        raise ValidationException(
            _("Department must belong to the teacher's branch."), code="department_branch_mismatch"
        )
    identity = prepare_role_identity(
        phone=phone, email=email, first_name=first_name, last_name=last_name, middle_name=middle_name
    )
    if not identity["phone"] and not identity["email"]:
        raise ValidationException(_("phone or email is required."), code="identifier_required")
    if (identity["phone"] and TeacherProfile.objects.filter(phone=identity["phone"]).exists()) or (
        identity["email"] and TeacherProfile.objects.filter(email__iexact=identity["email"]).exists()
    ):
        raise ValidationException(_("This person already has a teacher profile."), code="duplicate_teacher")
    user, username, identity = create_role_user_bridge(username=username, **identity)
    teacher = TeacherProfile.objects.create(
        user=user,
        branch=branch,
        department=department,
        # Identity and credentials are owned by the teacher account. The linked User is
        # an internal, password-disabled authorization bridge and is never operator-facing.
        username=username,
        password=user.password,
        first_name=identity["first_name"],
        last_name=identity["last_name"],
        middle_name=identity["middle_name"],
        phone=identity["phone"],
        email=identity["email"],
        birthdate=birthdate,
        gender=gender,
        hire_date=hire_date,
        subjects=subjects or [],
        qualifications=qualifications,
        salary_type=salary_type,
        rate=rate,
        is_substitute=is_substitute,
    )
    ensure_role_membership(
        teacher,
        branch=branch,
        department=department,
        role=Role.TEACHER if account_type is None else None,
        account_type=account_type,
    )
    return teacher


# ---------------------------------------------------------------------------
# F13-1 — dynamic payout policy + salary computation + A-1 salary-prep
# ---------------------------------------------------------------------------
def _money(raw, field: str) -> Decimal:
    """A positive, finite, 2-dp money amount, else a clean 400 (never a 500 / overflow)."""
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationException(
            _("Must be a number."), code="validation_error", fields={field: ["Must be a number."]}
        ) from None
    if not amount.is_finite() or amount <= 0 or amount >= Decimal("1e12"):
        raise ValidationException(
            _("Out of range."), code="validation_error", fields={field: ["Must be a positive amount."]}
        )
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValidationException(
            _("Use no more than two decimal places."),
            code="validation_error",
            fields={field: [_("Use no more than two decimal places.")]},
        )
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _percent(raw) -> Decimal:
    try:
        pct = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationException(
            _("Percent must be a number."),
            code="validation_error",
            fields={"tuition_percent": ["Must be a number."]},
        ) from None
    if not pct.is_finite() or not (Decimal("0") < pct <= Decimal("100")):
        raise ValidationException(
            _("Percent must be between 0 and 100."),
            code="validation_error",
            fields={"tuition_percent": ["0 < percent <= 100."]},
        )
    exponent = pct.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValidationException(
            _("Use no more than two decimal places."),
            code="validation_error",
            fields={"tuition_percent": [_("Use no more than two decimal places.")]},
        )
    return pct.quantize(_CENT, rounding=ROUND_HALF_UP)


@transaction.atomic
def set_payout_policy(
    *,
    teacher: TeacherProfile,
    method: str,
    hourly_rate_uzs=None,
    flat_amount_uzs=None,
    tuition_percent=None,
    is_active: bool = True,
) -> PayoutPolicy:
    """Create or replace a teacher's dynamic pay rule (F13-1). Validates that the params
    required by the chosen method are present + in range; irrelevant params are cleared so
    a policy can't carry stale values from a prior method."""
    # Serialize policy changes with salary preparation.  ``prepare_salary``
    # locks this same row before reading the policy, so a concurrent request can
    # never compute against a half-replaced or stale compensation rule.
    teacher = TeacherProfile.objects.select_for_update().get(pk=teacher.pk)
    if method not in PayoutPolicy.Method.values:
        raise ValidationException(
            _("Unknown payout method."),
            code="validation_error",
            fields={"method": [f"Must be one of {list(PayoutPolicy.Method.values)}."]},
        )
    fields: dict = {
        "method": method,
        "is_active": is_active,
        "hourly_rate_uzs": None,
        "flat_amount_uzs": None,
        "tuition_percent": None,
    }
    if method == PayoutPolicy.Method.HOURLY:
        fields["hourly_rate_uzs"] = _money(hourly_rate_uzs, "hourly_rate_uzs")
    elif method == PayoutPolicy.Method.FLAT_MONTHLY:
        fields["flat_amount_uzs"] = _money(flat_amount_uzs, "flat_amount_uzs")
    elif method == PayoutPolicy.Method.PERCENT_OF_TUITION:
        fields["tuition_percent"] = _percent(tuition_percent)
    policy, _created = PayoutPolicy.objects.update_or_create(teacher=teacher, defaults=fields)
    return policy


def _period_bounds(period_start: dt.date, period_end: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """[start 00:00, end+1day 00:00) as tz-aware datetimes — the period is inclusive of both
    the start and end dates."""
    if period_end < period_start:
        raise ValidationException(
            _("period_end must be on or after period_start."),
            code="validation_error",
            fields={"period_end": ["Must be on or after period_start."]},
        )
    if (period_end - period_start).days + 1 > _MAX_PAYOUT_PERIOD_DAYS:
        raise ValidationException(
            _("A payout period cannot exceed %(days)s days.") % {"days": _MAX_PAYOUT_PERIOD_DAYS},
            code="validation_error",
            fields={"period_end": [_("Choose a period of at most 366 days.")]},
        )
    from apps.org.selectors import get_center_settings

    try:
        tz = ZoneInfo(get_center_settings().organization_timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        # A corrupt raw-database setting must stop a money calculation rather
        # than silently falling back to the server or operator's timezone.
        raise UnprocessableEntity(
            _("The organization timezone is not configured correctly."),
            code="organization_timezone_invalid",
        ) from exc
    start_dt = timezone.make_aware(dt.datetime.combine(period_start, dt.time.min), tz)
    try:
        end_exclusive = period_end + dt.timedelta(days=1)
    except OverflowError:  # period_end at/near date.max — a clean 400, never a 500
        raise ValidationException(
            _("period_end is too far in the future."),
            code="validation_error",
            fields={"period_end": ["Out of range."]},
        ) from None
    end_dt = timezone.make_aware(dt.datetime.combine(end_exclusive, dt.time.min), tz)
    return start_dt, end_dt


def _teacher_cohort_ids(teacher: TeacherProfile):
    """Lazy cohort-id subquery for typed or legacy teaching responsibility."""
    from apps.cohorts.selectors import taught_cohorts

    # Percentage-of-tuition payout follows explicit cohort responsibility, not
    # a one-off/substitute lesson that could otherwise credit the whole
    # cohort's fees. Keep this lazy so a teacher with many historical cohorts
    # does not materialize an unbounded Python list.
    return (
        taught_cohorts(teacher=teacher, include_lesson_teacher=False).order_by().values_list("id", flat=True)
    )


def _validate_flat_month(period_start: dt.date, period_end: dt.date, *, tz: dt.tzinfo) -> None:
    """A flat-monthly amount is payable only for one completed calendar month."""
    next_month = (
        dt.date(period_start.year + 1, 1, 1)
        if period_start.month == 12
        else dt.date(period_start.year, period_start.month + 1, 1)
    )
    expected_end = next_month - dt.timedelta(days=1)
    if period_start.day != 1 or period_end != expected_end:
        raise ValidationException(
            _("A flat monthly payout requires one complete calendar month."),
            code="validation_error",
            fields={
                "period_start": [_("Use the first day of a calendar month.")],
                "period_end": [_("Use the final day of the same calendar month.")],
            },
        )
    organization_today = timezone.now().astimezone(tz).date()
    if period_end >= organization_today:
        raise ValidationException(
            _("A flat monthly payout can be prepared only after the month has ended."),
            code="validation_error",
            fields={"period_end": [_("Choose a completed calendar month.")]},
        )


def compute_payout(*, teacher: TeacherProfile, period_start: dt.date, period_end: dt.date) -> dict:
    """Compute what `teacher` is owed for the period under their active PayoutPolicy (F13-1).
    Returns {method, amount_uzs (Decimal, 2dp), breakdown} — a pure read, no side effects."""
    policy = PayoutPolicy.objects.filter(teacher=teacher, is_active=True).first()
    if policy is None:
        raise UnprocessableEntity(_("This teacher has no active payout policy."), code="no_payout_policy")
    start_dt, end_dt = _period_bounds(period_start, period_end)

    if policy.method == PayoutPolicy.Method.HOURLY:
        from apps.schedule.models import Lesson

        # Pay delivered work only.  Counting merely scheduled lessons lets a
        # future timetable be turned into an immediate cash liability.  Sum in
        # SQL so query count and Python memory remain constant as the period's
        # lesson count grows.
        duration = (
            Lesson.objects.filter(
                teacher=teacher,
                status=Lesson.Status.COMPLETED,
                starts_at__gte=start_dt,
                starts_at__lt=end_dt,
            ).aggregate(total=Sum(F("ends_at") - F("starts_at")))["total"]
            or dt.timedelta()
        )
        seconds = Decimal(duration.days * 86400 + duration.seconds) + Decimal(
            duration.microseconds
        ) / Decimal("1000000")
        exact_hours = seconds / _HOUR
        hours = exact_hours.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        hourly_rate = policy.hourly_rate_uzs
        if hourly_rate is None or not hourly_rate.is_finite() or hourly_rate <= 0:
            raise UnprocessableEntity(_("The active payout policy is invalid."), code="invalid_payout_policy")
        # Round the money once, after applying the rate to exact duration.  A
        # pre-rounded hour count can systematically over- or under-pay short
        # lessons across a large payroll.
        amount = (exact_hours * hourly_rate).quantize(_CENT, rounding=ROUND_HALF_UP)
        breakdown = {"hours": str(hours), "hourly_rate_uzs": str(hourly_rate)}

    elif policy.method == PayoutPolicy.Method.PERCENT_OF_TUITION:
        from apps.finance.models import PaymentAllocation

        # Tuition attributed PER COHORT the teacher teaches (Invoice.cohort), NOT by student
        # id — a student enrolled in another teacher's course too would otherwise credit
        # this teacher for tuition they paid for that OTHER course, so the total payout
        # could exceed the tuition actually collected.
        cohort_ids = _teacher_cohort_ids(teacher)
        collected = PaymentAllocation.objects.filter(
            invoice__cohort_id__in=cohort_ids,
            created_at__gte=start_dt,
            created_at__lt=end_dt,
        ).aggregate(total=Sum("amount_uzs"))["total"] or Decimal("0")
        tuition_percent = policy.tuition_percent
        if (
            tuition_percent is None
            or not tuition_percent.is_finite()
            or not (Decimal("0") < tuition_percent <= Decimal("100"))
        ):
            raise UnprocessableEntity(_("The active payout policy is invalid."), code="invalid_payout_policy")
        amount = (collected * tuition_percent / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)
        breakdown = {"collected_uzs": str(collected), "tuition_percent": str(tuition_percent)}

    elif policy.method == PayoutPolicy.Method.FLAT_MONTHLY:
        organization_tz = start_dt.tzinfo
        if organization_tz is None:  # pragma: no cover - make_aware guarantees this invariant
            raise UnprocessableEntity(
                _("The organization timezone is not configured correctly."),
                code="organization_timezone_invalid",
            )
        _validate_flat_month(period_start, period_end, tz=organization_tz)
        flat_amount = policy.flat_amount_uzs
        if flat_amount is None or not flat_amount.is_finite() or flat_amount <= 0:
            raise UnprocessableEntity(_("The active payout policy is invalid."), code="invalid_payout_policy")
        amount = flat_amount.quantize(_CENT, rounding=ROUND_HALF_UP)
        breakdown = {"flat_amount_uzs": str(flat_amount)}
    else:
        raise UnprocessableEntity(_("The active payout policy is invalid."), code="invalid_payout_policy")

    return {"method": policy.method, "amount_uzs": amount, "breakdown": breakdown}


def _salary_idempotency_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValidationException(
            _("Idempotency-Key must be a string."),
            code="validation_error",
            fields={"Idempotency-Key": [_("Must be a string.")]},
        )
    # Idempotency keys are opaque. Never trim or normalize them into a different
    # key; surrounding whitespace is invalid because only visible ASCII is
    # accepted below.
    value = raw
    if not 16 <= len(value) <= 128 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        raise ValidationException(
            _("Idempotency-Key must contain 16 to 128 visible ASCII characters."),
            code="validation_error",
            fields={
                "Idempotency-Key": [_("Use 16 to 128 visible ASCII characters.")],
            },
        )
    return value


def _existing_salary_request(
    *,
    idempotency_key_hash: str | None,
    domain_dedupe_key: str,
    operation_fingerprint: str,
):
    from apps.approvals.models import ApprovalRequest

    # An idempotency key identifies one operation independently of the domain
    # de-duplication key.  Resolve it first: an older request for the requested
    # teacher/period must never mask a newer row that already owns this key.
    # Otherwise reusing key K for operation B can incorrectly return B merely
    # because B has a lower primary key than operation A, which owns K.
    if idempotency_key_hash is not None:
        existing = ApprovalRequest.objects.filter(idempotency_key_hash=idempotency_key_hash).first()
        if existing is not None:
            if existing.operation_fingerprint != operation_fingerprint:
                raise ConflictException(
                    _("The idempotency key was already used for a different salary request."),
                    code="idempotency_mismatch",
                )
            return existing

    # The key is unused.  A separate key for the same logical teacher/period
    # still returns the existing request, without rebinding the new key to it.
    existing = ApprovalRequest.objects.filter(domain_dedupe_key=domain_dedupe_key).first()
    if existing is None:
        return None
    if existing.operation_fingerprint != operation_fingerprint:
        raise ConflictException(
            _("The idempotency key was already used for a different salary request."),
            code="idempotency_mismatch",
        )
    return existing


@transaction.atomic
def prepare_salary(
    *,
    teacher: TeacherProfile,
    period_start: dt.date,
    period_end: dt.date,
    requested_by=None,
    idempotency_key: str | None = None,
):
    """Compute the teacher's payout for the period and raise a salary-prep request through
    the A-1 approvals engine (F13-1). A manager approves it and a cashier disburses it — the
    teacher never approves or disburses their own pay (SoD, wired in approvals). Returns the
    created ApprovalRequest."""
    from apps.approvals.services import KIND_SALARY_PREP, create_request

    # This is the shared serialization lock for policy edits and all salary
    # periods belonging to one teacher. It closes both policy-read races and
    # concurrent overlapping-period preparation.
    teacher = TeacherProfile.objects.select_for_update().get(pk=teacher.pk)

    raw_key = _salary_idempotency_key(idempotency_key)
    actor_id = getattr(requested_by, "pk", None) or 0
    operation_fingerprint = stable_hash(
        f"salary:v1:{teacher.pk}:{period_start.isoformat()}:{period_end.isoformat()}"
    )
    domain_dedupe_key = stable_hash(f"{current_schema()}:{operation_fingerprint}")
    idempotency_key_hash = (
        stable_hash(f"{current_schema()}:{actor_id}:{raw_key}") if raw_key is not None else None
    )
    existing = _existing_salary_request(
        idempotency_key_hash=idempotency_key_hash,
        domain_dedupe_key=domain_dedupe_key,
        operation_fingerprint=operation_fingerprint,
    )
    if existing is not None:
        from apps.approvals.models import ApprovalRequest

        if existing.status in {
            ApprovalRequest.Status.REJECTED,
            ApprovalRequest.Status.CANCELLED,
        }:
            raise ConflictException(
                _("This exact salary period was already closed without payment."),
                code="salary_period_closed",
            )
        return existing

    from apps.approvals.models import ApprovalRequest

    overlapping = (
        ApprovalRequest.objects.filter(
            kind=KIND_SALARY_PREP,
            payload__teacher_profile_id=teacher.pk,
            payload__period_start__lte=period_end.isoformat(),
            payload__period_end__gte=period_start.isoformat(),
        )
        .exclude(
            status__in=(
                ApprovalRequest.Status.REJECTED,
                ApprovalRequest.Status.CANCELLED,
            )
        )
        .order_by("pk")
        .first()
    )
    if overlapping is not None:
        raise ConflictException(
            _("This salary period overlaps an existing request."),
            code="salary_period_overlap",
        )

    result = compute_payout(teacher=teacher, period_start=period_start, period_end=period_end)
    amount = result["amount_uzs"]
    if amount <= 0:
        raise UnprocessableEntity(
            _("The computed payout for this period is zero — nothing to prepare."),
            code="zero_payout",
        )
    payee = teacher.get_full_name() or f"teacher#{teacher.pk}"
    try:
        # ``create_request`` owns an inner savepoint.  A concurrent retry loses
        # one of the two unique constraints and can be safely reconciled below
        # without leaving this outer transaction broken.
        return create_request(
            kind=KIND_SALARY_PREP,
            title=f"Salary {period_start.isoformat()}..{period_end.isoformat()}: {payee}"[:200],
            requested_by=requested_by,
            amount_uzs=amount,
            branch=teacher.branch,
            payload={
                "teacher_profile_id": teacher.pk,
                "party_label": payee,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "method": result["method"],
                "breakdown": result["breakdown"],
            },
            idempotency_key_hash=idempotency_key_hash,
            operation_fingerprint=operation_fingerprint,
            domain_dedupe_key=domain_dedupe_key,
        )
    except IntegrityError:
        existing = _existing_salary_request(
            idempotency_key_hash=idempotency_key_hash,
            domain_dedupe_key=domain_dedupe_key,
            operation_fingerprint=operation_fingerprint,
        )
        if existing is None:  # pragma: no cover - unrelated database failure
            raise
        return existing
