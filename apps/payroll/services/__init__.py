"""Transactional payroll-period domain operations."""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import Case, F, Q, Sum, Value, When
from django.utils import timezone
from django.utils.html import escape
from django.utils.translation import gettext_lazy as _

from apps.payroll.dto import (
    AdjustmentCreateDTO,
    ExportCreateDTO,
    PaymentReconciliationDTO,
    PayrollPeriodCreateDTO,
    PreviewFilterDTO,
    ReversalDTO,
)
from apps.payroll.models import (
    PayrollAdjustment,
    PayrollAdjustmentEvent,
    PayrollExport,
    PayrollLineItem,
    PayrollPayslip,
    PayrollPeriod,
    PayrollPeriodEvent,
    PayrollReconciliation,
)
from core.exceptions import (
    ConflictException,
    NotFoundException,
    PermissionException,
    UnprocessableEntity,
    ValidationException,
)
from core.permissions import (
    get_user_roles_for_user,
    has_permission_code,
)
from core.role_principals import RolePrincipal
from core.scoping import permission_membership_scopes
from core.utils import current_schema, stable_hash

logger = logging.getLogger("starforge.payroll")

CENT = Decimal("0.01")
MAX_MONEY = Decimal("9999999999999999.99")
MAX_PERIOD_DAYS = 366
MAX_PAYROLL_TEACHERS = 500
MAX_PAYROLL_LESSON_GROUPS = 5_000
MAX_PAYROLL_ALLOCATION_GROUPS = 20_000
MAX_ACTIVE_EXPORTS = 10
DOWNLOAD_TTL_SECONDS = 600
_HOUR_SECONDS = Decimal("3600")


def _positive_id(value: Any, *, field: str, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationException(
            fields={field: [_("Choose a positive integer identifier.")]},
            code="validation_error",
        )
    return value


def _date_value(value: Any, *, field: str, optional: bool = False) -> dt.date | None:
    if value is None and optional:
        return None
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise ValidationException(
            fields={field: [_("Use an ISO date without a time component.")]},
            code="validation_error",
        )
    return value


def _assert_staff_actor(*, actor: Any, principal: RolePrincipal) -> None:
    actor_id = getattr(actor, "pk", None)
    if (
        not isinstance(principal, RolePrincipal)
        or principal.kind != "staff"
        or isinstance(principal.principal_id, bool)
        or not isinstance(principal.principal_id, int)
        or principal.principal_id <= 0
        or isinstance(actor_id, bool)
        or not isinstance(actor_id, int)
        or actor_id <= 0
        or principal.user_id != actor_id
        or not bool(getattr(actor, "is_active", False))
    ):
        raise PermissionException(
            _("This payroll operation requires an active staff role session."),
            code="principal_unavailable",
        )


def _money(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        raise ValidationException(
            fields={field: [_("Must be a decimal amount.")]}, code="validation_error"
        ) from None
    exponent = parsed.as_tuple().exponent
    if (
        not parsed.is_finite()
        or parsed < 0
        or (isinstance(exponent, int) and exponent < -2)
        or parsed > MAX_MONEY
    ):
        raise ValidationException(
            fields={field: [_("Use a non-negative amount with at most two decimal places.")]},
            code="validation_error",
        )
    return parsed.quantize(CENT)


def validate_idempotency_key(raw: str | None) -> str:
    if (
        not isinstance(raw, str)
        or not 16 <= len(raw) <= 128
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in raw)
    ):
        raise ValidationException(
            _("Idempotency-Key must contain 16 to 128 visible ASCII characters."),
            code="invalid_idempotency_key",
            fields={"Idempotency-Key": [_("Use 16 to 128 visible ASCII characters.")]},
        )
    return raw


def _key_hash(*, principal: RolePrincipal, raw: str) -> str:
    return stable_hash(f"payroll-key:v1:{current_schema()}:{principal.kind}:{principal.principal_id}:{raw}")


def _fingerprint(label: str, payload: dict[str, Any]) -> str:
    return stable_hash(
        f"payroll:{label}:v1:{json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)}"
    )


def _principal_equals(
    principal: RolePrincipal,
    *,
    kind: str,
    principal_id: int | None,
    user_id: int | None = None,
) -> bool:
    return (principal.kind == kind and principal.principal_id == principal_id) or (
        user_id is not None and principal.user_id == user_id
    )


def _scope_allowed(
    *,
    roles,
    permission: str,
    branch_id: int,
    department_id: int | None,
    is_superuser: bool = False,
) -> bool:
    if is_superuser:
        return True
    for scope in permission_membership_scopes(roles=roles, permission=permission):
        if scope.is_organization_wide:
            return True
        if scope.branch_id != branch_id:
            continue
        if scope.department_id is None or scope.department_id == department_id:
            return True
    return False


def _assert_scope(
    *,
    roles,
    permission: str,
    branch_id: int,
    department_id: int | None,
    is_superuser: bool = False,
) -> None:
    if not _scope_allowed(
        roles=roles,
        permission=permission,
        branch_id=branch_id,
        department_id=department_id,
        is_superuser=is_superuser,
    ):
        # Deliberately hide whether a branch/department exists outside scope.
        raise NotFoundException(code="not_found")


def _audit_period(
    period: PayrollPeriod,
    *,
    actor,
    principal: RolePrincipal,
    action: str,
    previous_status: str = "",
) -> None:
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_log

    audit_log(
        actor=actor,
        action=action,
        resource_type="payroll.PayrollPeriod",
        resource_id=period.pk,
        before={
            "status": previous_status,
            "branch_id": period.branch_id,
            "department_id": period.department_id,
        }
        if previous_status
        else None,
        after={
            "status": period.status,
            "branch_id": period.branch_id,
            "department_id": period.department_id,
            "line_count": period.line_count,
        },
        scope=scoped_audit_scope(period.branch_id, period.department_id),
        actor_principal=principal,
    )


def _audit_adjustment(
    adjustment: PayrollAdjustment,
    *,
    actor,
    principal: RolePrincipal,
    action: str,
    previous_state: str = "",
) -> None:
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_log

    audit_log(
        actor=actor,
        action=action,
        resource_type="payroll.PayrollAdjustment",
        resource_id=adjustment.pk,
        before={
            "state": previous_state,
            "branch_id": adjustment.branch_id,
            "department_id": adjustment.department_id,
        }
        if previous_state
        else None,
        after={
            "state": adjustment.state,
            "branch_id": adjustment.branch_id,
            "department_id": adjustment.department_id,
            "kind": adjustment.kind,
        },
        scope=scoped_audit_scope(adjustment.branch_id, adjustment.department_id),
        actor_principal=principal,
    )


def _assert_period_dates(start: dt.date, end: dt.date, pay_date: dt.date | None) -> None:
    if end < start:
        raise ValidationException(
            fields={"period_end": [_("Must be on or after period_start.")]},
            code="validation_error",
        )
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise ValidationException(
            fields={"period_end": [_("Choose a period of at most 366 days.")]},
            code="validation_error",
        )
    try:
        end + dt.timedelta(days=1)
    except OverflowError:
        raise ValidationException(
            fields={"period_end": [_("Out of range.")]},
            code="validation_error",
        ) from None
    if pay_date is not None and pay_date < end:
        raise ValidationException(
            fields={"pay_date": [_("Must be on or after period_end.")]},
            code="validation_error",
        )


def _validated_timezone_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name) > 64:
        raise UnprocessableEntity(
            _("The organization timezone is not configured correctly."),
            code="organization_timezone_invalid",
        )
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise UnprocessableEntity(
            _("The organization timezone is not configured correctly."),
            code="organization_timezone_invalid",
        ) from None
    return name


def _period_bounds(period: PayrollPeriod) -> tuple[dt.datetime, dt.datetime]:
    zone = ZoneInfo(_validated_timezone_name(period.organization_timezone))
    start = timezone.make_aware(
        dt.datetime.combine(period.period_start, dt.time.min),
        zone,
    )
    end = timezone.make_aware(
        dt.datetime.combine(period.period_end + dt.timedelta(days=1), dt.time.min),
        zone,
    )
    return start, end


@transaction.atomic
def create_period(
    *,
    dto: PayrollPeriodCreateDTO,
    actor,
    principal: RolePrincipal,
    roles,
) -> PayrollPeriod:
    from apps.org.models import Branch, Department
    from apps.org.selectors import get_center_settings

    _assert_staff_actor(actor=actor, principal=principal)
    branch_id = _positive_id(dto.branch_id, field="branch")
    department_id = _positive_id(dto.department_id, field="department", optional=True)
    correction_of_id = _positive_id(dto.correction_of_id, field="correction_of", optional=True)
    period_start = _date_value(dto.period_start, field="period_start")
    period_end = _date_value(dto.period_end, field="period_end")
    pay_date = _date_value(dto.pay_date, field="pay_date", optional=True)
    assert branch_id is not None
    assert period_start is not None
    assert period_end is not None
    _assert_period_dates(period_start, period_end, pay_date)
    organization_timezone = _validated_timezone_name(
        get_center_settings().organization_timezone,
    )
    label = dto.label.strip() if isinstance(dto.label, str) else ""
    correction_reason = dto.correction_reason.strip() if isinstance(dto.correction_reason, str) else ""
    if not label:
        raise ValidationException(fields={"label": [_("This field is required.")]}, code="validation_error")
    if len(label) > 120:
        raise ValidationException(
            fields={"label": [_("Must be at most 120 characters.")]},
            code="validation_error",
        )
    if len(correction_reason) > 255:
        raise ValidationException(
            fields={"correction_reason": [_("Must be at most 255 characters.")]},
            code="validation_error",
        )
    if dto.currency != "UZS":
        raise ValidationException(
            fields={"currency": [_("The current payroll contract supports UZS only.")]},
            code="validation_error",
        )
    branch = Branch.objects.select_for_update().filter(pk=branch_id, archived_at__isnull=True).first()
    if branch is None:
        raise NotFoundException(code="not_found")
    department = None
    if department_id is not None:
        department = Department.objects.filter(pk=department_id, branch=branch).first()
        if department is None:
            raise NotFoundException(code="not_found")
    _assert_scope(
        roles=roles,
        permission="compensation:run",
        branch_id=branch.pk,
        department_id=department.pk if department else None,
        is_superuser=bool(getattr(actor, "is_superuser", False)),
    )

    correction = None
    if correction_of_id is not None:
        correction = PayrollPeriod.objects.select_for_update().filter(pk=correction_of_id).first()
        if (
            correction is None
            or correction.status != PayrollPeriod.Status.REJECTED
            or correction.branch_id != branch.pk
            or correction.department_id != (department.pk if department else None)
            or correction.period_start != period_start
            or correction.period_end != period_end
            or correction.currency != dto.currency
        ):
            raise NotFoundException(code="not_found")
        # A correction must reproduce the rejected run's calendar boundary,
        # even if the organization changes its presentation timezone later.
        organization_timezone = _validated_timezone_name(
            correction.organization_timezone,
        )
        if not correction_reason:
            raise ValidationException(
                fields={"correction_reason": [_("Explain why the rejected run is being corrected.")]},
                code="validation_error",
            )
        if PayrollPeriod.objects.filter(correction_of=correction).exists():
            raise ConflictException(
                _("This rejected period already has a correction run."),
                code="payroll_correction_exists",
            )
    else:
        # A branch-wide period owns every department in that branch.  It must
        # therefore conflict with all overlapping department periods, while a
        # department period conflicts with its own department and any branch-
        # wide period.  Testing only exact department equality would permit the
        # same teacher to be paid twice for one window.
        scope_overlap = Q(department__isnull=True)
        if department is None:
            scope_overlap = Q()
        else:
            scope_overlap |= Q(department=department)
        overlaps = PayrollPeriod.objects.filter(
            scope_overlap,
            branch=branch,
            correction_of__isnull=True,
            period_start__lte=period_end,
            period_end__gte=period_start,
        ).exists()
        if overlaps:
            raise ConflictException(
                _("A payroll period already overlaps this scope and window."),
                code="payroll_period_overlap",
            )

    period = PayrollPeriod.objects.create(
        branch=branch,
        department=department,
        label=label,
        period_start=period_start,
        period_end=period_end,
        pay_date=pay_date,
        currency=dto.currency,
        organization_timezone=organization_timezone,
        correction_of=correction,
        correction_reason=correction_reason,
        version=(correction.version + 1) if correction is not None else 1,
        created_by=actor,
        created_principal_kind=principal.kind,
        created_principal_id=principal.principal_id,
    )
    _audit_period(period, actor=actor, principal=principal, action="create")
    return period


def _teacher_rows(period: PayrollPeriod, filters: PreviewFilterDTO, *, lock: bool) -> list[Any]:
    from apps.teachers.models import TeacherProfile

    if (
        len(filters.teacher_ids) > MAX_PAYROLL_TEACHERS
        or len(filters.teacher_ids) != len(set(filters.teacher_ids))
        or any(isinstance(pk, bool) or not isinstance(pk, int) or pk <= 0 for pk in filters.teacher_ids)
    ):
        raise ValidationException(
            fields={"teacher_ids": [_("Use at most 500 unique positive teacher IDs.")]},
            code="validation_error",
        )
    queryset = TeacherProfile.objects.filter(is_active=True, branch_id=period.branch_id)
    if period.department_id is not None:
        queryset = queryset.filter(department_id=period.department_id)
    if filters.teacher_ids:
        queryset = queryset.filter(pk__in=filters.teacher_ids)
    if lock:
        # ``department`` is nullable, so ``select_related`` emits an outer
        # join.  PostgreSQL cannot lock the nullable side of that join; lock
        # only the authoritative teacher rows used to serialize a run.
        queryset = queryset.select_for_update(of=("self",))
    teachers = list(
        queryset.select_related("user", "branch", "department").order_by("pk")[: MAX_PAYROLL_TEACHERS + 1]
    )
    if len(teachers) > MAX_PAYROLL_TEACHERS:
        raise ValidationException(
            _("This payroll scope exceeds the 500-teacher run limit."),
            code="payroll_scope_too_large",
            fields={"department": [_("Narrow the run to at most 500 teachers.")]},
        )
    requested = set(filters.teacher_ids)
    if requested and {teacher.pk for teacher in teachers} != requested:
        raise NotFoundException(code="not_found")
    if not teachers:
        raise UnprocessableEntity(
            _("No active teachers are in this payroll scope."), code="payroll_scope_empty"
        )
    return teachers


def _policy_snapshot(policy) -> dict[str, Any]:
    return {
        "id": policy.pk,
        "method": policy.method,
        "hourly_rate_uzs": str(policy.hourly_rate_uzs) if policy.hourly_rate_uzs is not None else None,
        "flat_amount_uzs": str(policy.flat_amount_uzs) if policy.flat_amount_uzs is not None else None,
        "tuition_percent": str(policy.tuition_percent) if policy.tuition_percent is not None else None,
        "updated_at": policy.updated_at.isoformat(),
    }


def _preview(period: PayrollPeriod, filters: PreviewFilterDTO, *, lock: bool = False) -> dict[str, Any]:
    from apps.finance.models import InvoiceLine, PaymentAllocation
    from apps.schedule.models import Lesson
    from apps.teachers.models import PayoutPolicy
    from apps.teachers.services import _validate_flat_month

    teachers = _teacher_rows(period, filters, lock=lock)
    teacher_ids = [teacher.pk for teacher in teachers]
    policies_qs = PayoutPolicy.objects.filter(teacher_id__in=teacher_ids, is_active=True)
    if lock:
        policies_qs = policies_qs.select_for_update()
    policies = {policy.teacher_id: policy for policy in policies_qs.order_by("teacher_id")}
    start_dt, end_dt = _period_bounds(period)

    # Completed lessons are immutable delivery evidence for this window.  They
    # also provide historical cohort attribution for percentage-of-tuition
    # policies.  Current CohortTeacher/primary_teacher assignments have no
    # effective dates and would let a later reassignment rewrite old payroll.
    lesson_groups = cast(
        list[tuple[int, int, dt.timedelta]],
        list(
            Lesson.objects.filter(
                teacher_id__in=teacher_ids,
                status=Lesson.Status.COMPLETED,
                starts_at__gte=start_dt,
                starts_at__lt=end_dt,
            )
            .values("teacher_id", "cohort_id")
            .annotate(total=Sum(F("ends_at") - F("starts_at")))
            .order_by("teacher_id", "cohort_id")
            .values_list("teacher_id", "cohort_id", "total")[: MAX_PAYROLL_LESSON_GROUPS + 1]
        ),
    )
    if len(lesson_groups) > MAX_PAYROLL_LESSON_GROUPS:
        raise UnprocessableEntity(
            _("This payroll window contains too many teacher/cohort delivery groups."),
            code="payroll_evidence_too_large",
        )
    durations: dict[int, dt.timedelta] = defaultdict(dt.timedelta)
    cohorts_by_teacher: dict[int, set[int]] = defaultdict(set)
    for teacher_id, cohort_id, duration in lesson_groups:
        durations[teacher_id] += duration
        cohorts_by_teacher[teacher_id].add(cohort_id)
    all_cohort_ids = {cohort_id for values in cohorts_by_teacher.values() for cohort_id in values}
    allocation_groups = cast(
        list[tuple[int, int, Decimal]],
        list(
            PaymentAllocation.objects.filter(
                invoice__cohort_id__in=all_cohort_ids,
                created_at__gte=start_dt,
                created_at__lt=end_dt,
            )
            .values("invoice_id", "invoice__cohort_id")
            .annotate(total=Sum("amount_uzs"))
            .order_by("invoice_id")
            .values_list("invoice_id", "invoice__cohort_id", "total")[: MAX_PAYROLL_ALLOCATION_GROUPS + 1]
        ),
    )
    if len(allocation_groups) > MAX_PAYROLL_ALLOCATION_GROUPS:
        raise UnprocessableEntity(
            _("This payroll window contains too many tuition-allocation groups."),
            code="payroll_evidence_too_large",
        )
    allocation_invoice_ids = [invoice_id for invoice_id, _cohort_id, _total in allocation_groups]
    invoice_line_kinds: dict[int, set[str]] = defaultdict(set)
    for invoice_id, line_type in (
        InvoiceLine.objects.filter(
            invoice_id__in=allocation_invoice_ids,
        )
        .order_by()
        .values_list("invoice_id", "line_type")
        .distinct()
    ):
        invoice_line_kinds[invoice_id].add(line_type)

    # The finance model cannot attribute a payment allocation to individual
    # lines.  Counting an allocation from a mixed tuition/material/penalty
    # invoice as tuition would overpay a percentage policy.  Pure
    # tuition+discount invoices are unambiguous; every mixed or line-less
    # invoice fails the affected teacher calculation closed until finance owns
    # a line-allocation contract.
    allowed_tuition_lines = {InvoiceLine.LineType.TUITION, InvoiceLine.LineType.DISCOUNT}
    collected_by_cohort: dict[int, Decimal] = defaultdict(Decimal)
    ambiguous_tuition_cohorts: set[int] = set()
    for invoice_id, cohort_id, total in allocation_groups:
        line_types = invoice_line_kinds[invoice_id]
        if InvoiceLine.LineType.TUITION not in line_types or not line_types.issubset(allowed_tuition_lines):
            ambiguous_tuition_cohorts.add(cohort_id)
            continue
        collected_by_cohort[cohort_id] += total

    adjustments_qs = PayrollAdjustment.objects.filter(
        teacher_id__in=teacher_ids,
        branch_id=period.branch_id,
        currency=period.currency,
        effective_period_start=period.period_start,
        effective_period_end=period.period_end,
        state=PayrollAdjustment.State.APPROVED,
        applied_line__isnull=True,
    )
    if period.department_id is not None:
        adjustments_qs = adjustments_qs.filter(department_id=period.department_id)
    adjustments_qs = adjustments_qs.order_by("pk")
    if lock:
        adjustments_qs = adjustments_qs.select_for_update()
    adjustments = list(adjustments_qs)
    adjustment_totals: dict[int, dict[str, Decimal]] = defaultdict(
        lambda: {"bonus": Decimal("0"), "deduction": Decimal("0")}
    )
    adjustment_ids: dict[int, list[int]] = defaultdict(list)
    for adjustment in adjustments:
        adjustment_totals[adjustment.teacher_id][adjustment.kind] += adjustment.amount_uzs
        adjustment_ids[adjustment.teacher_id].append(adjustment.pk)

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for teacher in teachers:
        policy = policies.get(teacher.pk)
        if policy is None:
            errors.append({"teacher": teacher.pk, "code": "no_payout_policy"})
            continue
        breakdown: dict[str, Any]
        try:
            if policy.method == PayoutPolicy.Method.HOURLY:
                duration = durations.get(teacher.pk) or dt.timedelta()
                seconds = Decimal(duration.days * 86400 + duration.seconds) + Decimal(
                    duration.microseconds
                ) / Decimal("1000000")
                hours = seconds / _HOUR_SECONDS
                rate = policy.hourly_rate_uzs
                if rate is None or not rate.is_finite() or rate <= 0:
                    raise ValueError("invalid policy")
                base = (hours * rate).quantize(CENT, rounding=ROUND_HALF_UP)
                breakdown = {
                    "hours": str(hours.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
                    "hourly_rate_uzs": str(rate),
                }
            elif policy.method == PayoutPolicy.Method.PERCENT_OF_TUITION:
                percent = policy.tuition_percent
                if percent is None or not percent.is_finite() or not Decimal("0") < percent <= Decimal("100"):
                    raise ValueError("invalid policy")
                if cohorts_by_teacher[teacher.pk] & ambiguous_tuition_cohorts:
                    errors.append(
                        {
                            "teacher": teacher.pk,
                            "code": "ambiguous_tuition_allocation",
                        }
                    )
                    continue
                collected = sum(
                    (collected_by_cohort.get(cohort_id) or Decimal("0"))
                    for cohort_id in cohorts_by_teacher[teacher.pk]
                )
                base = (collected * percent / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
                breakdown = {
                    "collected_uzs": str(collected),
                    "tuition_percent": str(percent),
                    "cohort_count": len(cohorts_by_teacher[teacher.pk]),
                    "attribution": "completed_lesson_cohorts",
                }
            elif policy.method == PayoutPolicy.Method.FLAT_MONTHLY:
                assert start_dt.tzinfo is not None
                _validate_flat_month(period.period_start, period.period_end, tz=start_dt.tzinfo)
                amount = policy.flat_amount_uzs
                if amount is None or not amount.is_finite() or amount <= 0:
                    raise ValueError("invalid policy")
                base = amount.quantize(CENT, rounding=ROUND_HALF_UP)
                breakdown = {"flat_amount_uzs": str(amount)}
            else:
                raise ValueError("invalid policy")
        except (ArithmeticError, ValueError, ValidationException, UnprocessableEntity) as exc:
            errors.append(
                {
                    "teacher": teacher.pk,
                    "code": getattr(exc, "code", "invalid_payout_policy"),
                }
            )
            continue

        bonus = adjustment_totals[teacher.pk][PayrollAdjustment.Kind.BONUS].quantize(CENT)
        deduction = adjustment_totals[teacher.pk][PayrollAdjustment.Kind.DEDUCTION].quantize(CENT)
        net = (base + bonus - deduction).quantize(CENT)
        if any(value > MAX_MONEY for value in (base, bonus, deduction, net)):
            errors.append({"teacher": teacher.pk, "code": "calculation_amount_too_large"})
            continue
        if net < 0:
            errors.append({"teacher": teacher.pk, "code": "deductions_exceed_pay"})
            continue
        rows.append(
            {
                "teacher": teacher,
                "policy": policy,
                "policy_snapshot": _policy_snapshot(policy),
                "breakdown": breakdown,
                "base_amount_uzs": base,
                "bonus_amount_uzs": bonus,
                "deduction_amount_uzs": deduction,
                "net_amount_uzs": net,
                "adjustment_ids": tuple(adjustment_ids[teacher.pk]),
            }
        )

    totals = {
        "base_total_uzs": sum((row["base_amount_uzs"] for row in rows), Decimal("0")),
        "bonus_total_uzs": sum((row["bonus_amount_uzs"] for row in rows), Decimal("0")),
        "deduction_total_uzs": sum(
            (row["deduction_amount_uzs"] for row in rows),
            Decimal("0"),
        ),
        "net_total_uzs": sum((row["net_amount_uzs"] for row in rows), Decimal("0")),
    }
    if any(total > MAX_MONEY for total in totals.values()):
        raise UnprocessableEntity(
            _("This payroll total exceeds the supported monetary range."),
            code="payroll_total_too_large",
        )
    return {
        "period": period,
        "rows": rows,
        "errors": errors,
        "valid": not errors,
        "teacher_count": len(teachers),
        **totals,
    }


def preview_period(*, period: PayrollPeriod, filters: PreviewFilterDTO) -> dict[str, Any]:
    if period.status != PayrollPeriod.Status.DRAFT:
        raise ConflictException(
            _("Only a draft payroll period can be previewed."), code="payroll_period_frozen"
        )
    return _preview(period, filters)


@transaction.atomic
def run_period(
    *,
    period: PayrollPeriod,
    filters: PreviewFilterDTO,
    actor,
    principal: RolePrincipal,
    idempotency_key: str,
) -> PayrollPeriod:
    from apps.org.models import Branch

    _assert_staff_actor(actor=actor, principal=principal)
    raw_key = validate_idempotency_key(idempotency_key)
    key_hash = _key_hash(principal=principal, raw=raw_key)
    fingerprint = _fingerprint(
        "period-run",
        {
            "period": period.pk,
            "teacher_ids": sorted(filters.teacher_ids),
            "version": period.version,
        },
    )
    # Branch-first lock ordering is shared with period creation and gives
    # overlapping create/run operations one deterministic serialization point.
    Branch.objects.select_for_update().get(pk=period.branch_id)
    locked = PayrollPeriod.objects.select_for_update().get(pk=period.pk)
    owner = PayrollPeriod.objects.filter(run_idempotency_key_hash=key_hash).first()
    if owner is not None:
        if owner.pk != locked.pk or owner.run_fingerprint != fingerprint:
            raise ConflictException(
                _("This idempotency key belongs to another payroll operation."),
                code="idempotency_key_reused",
            )
        return owner
    if locked.status != PayrollPeriod.Status.DRAFT:
        raise ConflictException(
            _("This payroll period has already been frozen."), code="payroll_period_frozen"
        )
    _start_dt, end_dt = _period_bounds(locked)
    organization_today = timezone.now().astimezone(end_dt.tzinfo).date()
    if locked.period_end >= organization_today:
        raise UnprocessableEntity(
            _("Payroll can be frozen only after the period has ended."),
            code="payroll_period_incomplete",
        )

    result = _preview(locked, filters, lock=True)
    if not result["valid"]:
        teacher_errors = {str(item["teacher"]): [item["code"]] for item in result["errors"]}
        raise UnprocessableEntity(
            _("Resolve every teacher calculation before freezing payroll."),
            code="payroll_preview_invalid",
            fields={"teachers": teacher_errors},
        )

    lines = [
        PayrollLineItem(
            period=locked,
            teacher=row["teacher"],
            branch_at_run_id=row["teacher"].branch_id,
            department_at_run_id=row["teacher"].department_id,
            teacher_user_id_snapshot=row["teacher"].user_id,
            teacher_name_snapshot=(row["teacher"].get_full_name() or row["teacher"].username)[:255],
            teacher_code_snapshot=(row["teacher"].username or f"teacher-{row['teacher'].pk}")[:150],
            payout_policy_id_snapshot=row["policy"].pk,
            payout_method_snapshot=row["policy"].method,
            payout_policy_snapshot=row["policy_snapshot"],
            calculation_breakdown=row["breakdown"],
            currency=locked.currency,
            base_amount_uzs=row["base_amount_uzs"],
            bonus_amount_uzs=row["bonus_amount_uzs"],
            deduction_amount_uzs=row["deduction_amount_uzs"],
            net_amount_uzs=row["net_amount_uzs"],
        )
        for row in result["rows"]
    ]
    PayrollLineItem.objects.bulk_create(lines)
    line_by_teacher = {line.teacher_id: line for line in lines}
    PayrollPayslip.objects.bulk_create(
        [
            PayrollPayslip(
                line_item=line,
                document_number=f"PAY-{locked.pk:08d}-{line.pk:08d}",
                snapshot={
                    "period": {
                        "id": locked.pk,
                        "label": locked.label,
                        "period_start": locked.period_start.isoformat(),
                        "period_end": locked.period_end.isoformat(),
                        "pay_date": locked.pay_date.isoformat() if locked.pay_date else None,
                        "organization_timezone": locked.organization_timezone,
                    },
                    "teacher": {
                        "id": line.teacher_id,
                        "code": line.teacher_code_snapshot,
                        "name": line.teacher_name_snapshot,
                    },
                    "currency": locked.currency,
                    "base_amount_uzs": str(line.base_amount_uzs),
                    "bonus_amount_uzs": str(line.bonus_amount_uzs),
                    "deduction_amount_uzs": str(line.deduction_amount_uzs),
                    "net_amount_uzs": str(line.net_amount_uzs),
                    "calculation": line.calculation_breakdown,
                    "payout_policy": line.payout_policy_snapshot,
                },
            )
            for line in lines
        ]
    )

    adjustment_ids = [pk for row in result["rows"] for pk in row["adjustment_ids"]]
    applied_adjustments = list(
        PayrollAdjustment.objects.select_for_update().filter(
            pk__in=adjustment_ids,
            state=PayrollAdjustment.State.APPROVED,
            applied_line__isnull=True,
        )
    )
    if len(applied_adjustments) != len(adjustment_ids):
        raise ConflictException(
            _("An adjustment changed while payroll was being frozen."),
            code="payroll_adjustment_changed",
        )
    for adjustment in applied_adjustments:
        adjustment.state = PayrollAdjustment.State.APPLIED
        adjustment.applied_line = line_by_teacher[adjustment.teacher_id]
        adjustment.save(update_fields=("state", "applied_line"))
        PayrollAdjustmentEvent.objects.create(
            adjustment=adjustment,
            action=PayrollAdjustmentEvent.Action.APPLIED,
            actor=actor,
            actor_principal_kind=principal.kind,
            actor_principal_id=principal.principal_id,
            note=f"period:{locked.pk}",
        )

    previous_status = locked.status
    locked.status = PayrollPeriod.Status.PENDING_APPROVAL
    locked.run_by = actor
    locked.run_principal_kind = principal.kind
    locked.run_principal_id = principal.principal_id
    locked.run_idempotency_key_hash = key_hash
    locked.run_fingerprint = fingerprint
    locked.line_count = len(lines)
    locked.base_total_uzs = result["base_total_uzs"]
    locked.bonus_total_uzs = result["bonus_total_uzs"]
    locked.deduction_total_uzs = result["deduction_total_uzs"]
    locked.net_total_uzs = result["net_total_uzs"]
    locked.frozen_at = timezone.now()
    locked.save(
        update_fields=(
            "status",
            "run_by",
            "run_principal_kind",
            "run_principal_id",
            "run_idempotency_key_hash",
            "run_fingerprint",
            "line_count",
            "base_total_uzs",
            "bonus_total_uzs",
            "deduction_total_uzs",
            "net_total_uzs",
            "frozen_at",
            "updated_at",
        )
    )
    PayrollPeriodEvent.objects.create(
        period=locked,
        action=PayrollPeriodEvent.Action.RUN,
        actor=actor,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    _audit_period(
        locked,
        actor=actor,
        principal=principal,
        action="update",
        previous_status=previous_status,
    )
    return locked


def _action_replay(
    *,
    period: PayrollPeriod,
    action: str,
    key_hash: str,
    fingerprint: str,
) -> PayrollPeriod | None:
    event = PayrollPeriodEvent.objects.select_related("period").filter(idempotency_key_hash=key_hash).first()
    if event is None:
        return None
    if event.period_id != period.pk or event.action != action or event.operation_fingerprint != fingerprint:
        raise ConflictException(
            _("This idempotency key belongs to another payroll operation."),
            code="idempotency_key_reused",
        )
    return event.period


@transaction.atomic
def approve_period(
    *,
    period: PayrollPeriod,
    actor,
    principal: RolePrincipal,
    note: str,
    idempotency_key: str,
) -> PayrollPeriod:
    _assert_staff_actor(actor=actor, principal=principal)
    normalized_note = note.strip() if isinstance(note, str) else ""
    if len(normalized_note) > 255:
        raise ValidationException(
            fields={"note": [_("Must be at most 255 characters.")]},
            code="validation_error",
        )
    raw = validate_idempotency_key(idempotency_key)
    key_hash = _key_hash(principal=principal, raw=raw)
    fingerprint = _fingerprint(
        "period-approve",
        {"period": period.pk, "note": normalized_note, "version": period.version},
    )
    locked = PayrollPeriod.objects.select_for_update().get(pk=period.pk)
    replay = _action_replay(
        period=locked,
        action=PayrollPeriodEvent.Action.APPROVE,
        key_hash=key_hash,
        fingerprint=fingerprint,
    )
    if replay is not None:
        return replay
    if locked.status != PayrollPeriod.Status.PENDING_APPROVAL:
        raise ConflictException(
            _("Only a pending payroll period can be approved."),
            code="payroll_period_not_pending",
        )
    if _principal_equals(
        principal,
        kind=locked.run_principal_kind,
        principal_id=locked.run_principal_id,
        user_id=locked.run_by_id,
    ) or _principal_equals(
        principal,
        kind=locked.created_principal_kind,
        principal_id=locked.created_principal_id,
        user_id=locked.created_by_id,
    ):
        raise PermissionException(
            _("The payroll maker cannot approve the same period."),
            code="payroll_self_approval",
        )
    previous_status = locked.status
    locked.status = (
        PayrollPeriod.Status.PAID if locked.net_total_uzs == Decimal("0") else PayrollPeriod.Status.APPROVED
    )
    locked.approved_by = actor
    locked.approved_principal_kind = principal.kind
    locked.approved_principal_id = principal.principal_id
    locked.decision_note = normalized_note
    locked.decided_at = timezone.now()
    locked.save(
        update_fields=(
            "status",
            "approved_by",
            "approved_principal_kind",
            "approved_principal_id",
            "decision_note",
            "decided_at",
            "updated_at",
        )
    )
    PayrollPeriodEvent.objects.create(
        period=locked,
        action=PayrollPeriodEvent.Action.APPROVE,
        actor=actor,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        note=normalized_note,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    _audit_period(
        locked,
        actor=actor,
        principal=principal,
        action="update",
        previous_status=previous_status,
    )
    return locked


@transaction.atomic
def reject_period(
    *,
    period: PayrollPeriod,
    actor,
    principal: RolePrincipal,
    note: str,
    idempotency_key: str,
) -> PayrollPeriod:
    _assert_staff_actor(actor=actor, principal=principal)
    normalized_note = note.strip() if isinstance(note, str) else ""
    if not normalized_note:
        raise ValidationException(
            fields={"note": [_("A rejection reason is required.")]}, code="validation_error"
        )
    if len(normalized_note) > 255:
        raise ValidationException(
            fields={"note": [_("Must be at most 255 characters.")]},
            code="validation_error",
        )
    raw = validate_idempotency_key(idempotency_key)
    key_hash = _key_hash(principal=principal, raw=raw)
    fingerprint = _fingerprint(
        "period-reject",
        {"period": period.pk, "note": normalized_note, "version": period.version},
    )
    locked = PayrollPeriod.objects.select_for_update().get(pk=period.pk)
    replay = _action_replay(
        period=locked,
        action=PayrollPeriodEvent.Action.REJECT,
        key_hash=key_hash,
        fingerprint=fingerprint,
    )
    if replay is not None:
        return replay
    if locked.status != PayrollPeriod.Status.PENDING_APPROVAL:
        raise ConflictException(
            _("Only a pending payroll period can be rejected."),
            code="payroll_period_not_pending",
        )
    if _principal_equals(
        principal,
        kind=locked.run_principal_kind,
        principal_id=locked.run_principal_id,
        user_id=locked.run_by_id,
    ) or _principal_equals(
        principal,
        kind=locked.created_principal_kind,
        principal_id=locked.created_principal_id,
        user_id=locked.created_by_id,
    ):
        raise PermissionException(
            _("The payroll maker cannot decide the same period."),
            code="payroll_self_approval",
        )
    previous_status = locked.status
    locked.status = PayrollPeriod.Status.REJECTED
    locked.rejected_by = actor
    locked.rejected_principal_kind = principal.kind
    locked.rejected_principal_id = principal.principal_id
    locked.decision_note = normalized_note
    locked.decided_at = timezone.now()
    locked.save(
        update_fields=(
            "status",
            "rejected_by",
            "rejected_principal_kind",
            "rejected_principal_id",
            "decision_note",
            "decided_at",
            "updated_at",
        )
    )

    # Rejection does not destroy frozen lines.  It merely releases adjustments
    # for the one explicitly linked correction period.
    adjustments = list(
        PayrollAdjustment.objects.select_for_update().filter(
            applied_line__period=locked,
            state=PayrollAdjustment.State.APPLIED,
        )
    )
    for adjustment in adjustments:
        adjustment.state = PayrollAdjustment.State.APPROVED
        adjustment.applied_line = None
        adjustment.save(update_fields=("state", "applied_line"))
        PayrollAdjustmentEvent.objects.create(
            adjustment=adjustment,
            action=PayrollAdjustmentEvent.Action.RELEASED,
            actor=actor,
            actor_principal_kind=principal.kind,
            actor_principal_id=principal.principal_id,
            note=f"rejected-period:{locked.pk}",
        )

    PayrollPeriodEvent.objects.create(
        period=locked,
        action=PayrollPeriodEvent.Action.REJECT,
        actor=actor,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        note=normalized_note,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    _audit_period(
        locked,
        actor=actor,
        principal=principal,
        action="update",
        previous_status=previous_status,
    )
    return locked


@transaction.atomic
def create_adjustment(
    *,
    dto: AdjustmentCreateDTO,
    actor,
    principal: RolePrincipal,
    roles,
) -> PayrollAdjustment:
    from apps.teachers.models import TeacherProfile

    _assert_staff_actor(actor=actor, principal=principal)
    teacher_id = _positive_id(dto.teacher_id, field="teacher")
    period_start = _date_value(
        dto.effective_period_start,
        field="effective_period_start",
    )
    period_end = _date_value(
        dto.effective_period_end,
        field="effective_period_end",
    )
    assert teacher_id is not None
    assert period_start is not None
    assert period_end is not None
    raw = validate_idempotency_key(dto.idempotency_key)
    if dto.kind not in PayrollAdjustment.Kind.values:
        raise ValidationException(fields={"kind": [_("Choose bonus or deduction.")]}, code="validation_error")
    amount = _money(dto.amount_uzs, field="amount_uzs")
    if amount <= 0:
        raise ValidationException(
            fields={"amount_uzs": [_("Must be greater than zero.")]}, code="validation_error"
        )
    if dto.currency != "UZS":
        raise ValidationException(
            fields={"currency": [_("The current payroll contract supports UZS only.")]},
            code="validation_error",
        )
    _assert_period_dates(period_start, period_end, None)
    reason = dto.reason.strip() if isinstance(dto.reason, str) else ""
    if not reason:
        raise ValidationException(fields={"reason": [_("A reason is required.")]}, code="validation_error")
    if len(reason) > 255:
        raise ValidationException(
            fields={"reason": [_("Must be at most 255 characters.")]},
            code="validation_error",
        )
    key_hash = _key_hash(principal=principal, raw=raw)
    fingerprint = _fingerprint(
        "adjustment-create",
        {
            "teacher": teacher_id,
            "kind": dto.kind,
            "amount_uzs": str(amount),
            "currency": dto.currency,
            "effective_period_start": period_start,
            "effective_period_end": period_end,
            "reason": reason,
        },
    )
    existing = PayrollAdjustment.objects.filter(idempotency_key_hash=key_hash).first()
    if existing is not None:
        if existing.operation_fingerprint != fingerprint:
            raise ConflictException(code="idempotency_key_reused")
        return existing
    teacher = (
        TeacherProfile.objects.select_for_update(of=("self",))
        .select_related("branch", "department")
        .filter(pk=teacher_id, is_active=True)
        .first()
    )
    if teacher is None:
        raise NotFoundException(code="not_found")
    # The teacher lock serializes adjustment creation with payroll freeze.  A
    # second identical request may have waited here while the first committed,
    # so resolve it again before attempting the unique insert.
    existing = PayrollAdjustment.objects.filter(idempotency_key_hash=key_hash).first()
    if existing is not None:
        if existing.operation_fingerprint != fingerprint:
            raise ConflictException(code="idempotency_key_reused")
        return existing
    _assert_scope(
        roles=roles,
        permission="compensation:write",
        branch_id=teacher.branch_id,
        department_id=teacher.department_id,
        is_superuser=bool(getattr(actor, "is_superuser", False)),
    )
    if (
        PayrollPeriod.objects.filter(
            Q(department__isnull=True) | Q(department_id=teacher.department_id),
            branch_id=teacher.branch_id,
            period_start=period_start,
            period_end=period_end,
        )
        .exclude(status=PayrollPeriod.Status.DRAFT)
        .exists()
    ):
        raise ConflictException(
            _("That payroll window is already frozen; create a later correcting adjustment."),
            code="adjustment_period_frozen",
        )
    adjustment = PayrollAdjustment.objects.create(
        teacher=teacher,
        branch_id=teacher.branch_id,
        department_id=teacher.department_id,
        kind=dto.kind,
        amount_uzs=amount,
        currency=dto.currency,
        effective_period_start=period_start,
        effective_period_end=period_end,
        reason=reason,
        created_by=actor,
        created_principal_kind=principal.kind,
        created_principal_id=principal.principal_id,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    PayrollAdjustmentEvent.objects.create(
        adjustment=adjustment,
        action=PayrollAdjustmentEvent.Action.CREATED,
        actor=actor,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
    )
    _audit_adjustment(adjustment, actor=actor, principal=principal, action="create")
    return adjustment


@transaction.atomic
def decide_adjustment(
    *,
    adjustment: PayrollAdjustment,
    approve: bool,
    actor,
    principal: RolePrincipal,
    note: str,
    idempotency_key: str,
) -> PayrollAdjustment:
    _assert_staff_actor(actor=actor, principal=principal)
    raw = validate_idempotency_key(idempotency_key)
    key_hash = _key_hash(principal=principal, raw=raw)
    action = PayrollAdjustmentEvent.Action.APPROVED if approve else PayrollAdjustmentEvent.Action.REJECTED
    normalized_note = note.strip() if isinstance(note, str) else ""
    if len(normalized_note) > 255:
        raise ValidationException(
            fields={"note": [_("Must be at most 255 characters.")]},
            code="validation_error",
        )
    if not approve and not normalized_note:
        raise ValidationException(
            fields={"note": [_("A rejection reason is required.")]},
            code="validation_error",
        )
    fingerprint = _fingerprint(
        f"adjustment-{action}",
        {"adjustment": adjustment.pk, "note": normalized_note},
    )
    existing = (
        PayrollAdjustmentEvent.objects.select_related("adjustment")
        .filter(idempotency_key_hash=key_hash)
        .first()
    )
    if existing is not None:
        if (
            existing.adjustment_id != adjustment.pk
            or existing.action != action
            or existing.operation_fingerprint != fingerprint
        ):
            raise ConflictException(code="idempotency_key_reused")
        return existing.adjustment

    # Teacher-first lock ordering matches period run and prevents an approval
    # from arriving just after the run took its adjustment snapshot.
    from apps.teachers.models import TeacherProfile

    TeacherProfile.objects.select_for_update().get(pk=adjustment.teacher_id)
    locked = PayrollAdjustment.objects.select_for_update().get(pk=adjustment.pk)
    existing = (
        PayrollAdjustmentEvent.objects.select_related("adjustment")
        .filter(idempotency_key_hash=key_hash)
        .first()
    )
    if existing is not None:
        if (
            existing.adjustment_id != locked.pk
            or existing.action != action
            or existing.operation_fingerprint != fingerprint
        ):
            raise ConflictException(code="idempotency_key_reused")
        return existing.adjustment
    if locked.state != PayrollAdjustment.State.PENDING:
        raise ConflictException(_("Only a pending adjustment can be decided."), code="adjustment_not_pending")
    if _principal_equals(
        principal,
        kind=locked.created_principal_kind,
        principal_id=locked.created_principal_id,
        user_id=locked.created_by_id,
    ):
        raise PermissionException(
            _("The adjustment creator cannot approve or reject it."),
            code="adjustment_self_approval",
        )
    if (
        PayrollPeriod.objects.filter(
            Q(department__isnull=True) | Q(department_id=locked.department_id),
            branch_id=locked.branch_id,
            period_start=locked.effective_period_start,
            period_end=locked.effective_period_end,
        )
        .exclude(status=PayrollPeriod.Status.DRAFT)
        .exists()
    ):
        raise ConflictException(
            _("The matching payroll period is already frozen."),
            code="adjustment_period_frozen",
        )
    previous_state = locked.state
    locked.state = PayrollAdjustment.State.APPROVED if approve else PayrollAdjustment.State.REJECTED
    locked.decided_by = actor
    locked.decided_principal_kind = principal.kind
    locked.decided_principal_id = principal.principal_id
    locked.decided_at = timezone.now()
    locked.decision_reason = normalized_note
    locked.save(
        update_fields=(
            "state",
            "decided_by",
            "decided_principal_kind",
            "decided_principal_id",
            "decided_at",
            "decision_reason",
        )
    )
    PayrollAdjustmentEvent.objects.create(
        adjustment=locked,
        action=action,
        actor=actor,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        note=normalized_note,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    _audit_adjustment(
        locked,
        actor=actor,
        principal=principal,
        action="update",
        previous_state=previous_state,
    )
    return locked


def _line_paid_total(line: PayrollLineItem) -> Decimal:
    value = line.reconciliations.aggregate(
        total=Sum(
            Case(
                When(
                    kind=PayrollReconciliation.Kind.PAYMENT,
                    then=F("amount_uzs"),
                ),
                When(
                    kind=PayrollReconciliation.Kind.REVERSAL,
                    then=-F("amount_uzs"),
                ),
                default=Value(Decimal("0")),
            )
        )
    )["total"]
    return (value or Decimal("0")).quantize(CENT)


def _assert_disburser_separation(
    *, period: PayrollPeriod, line: PayrollLineItem, principal: RolePrincipal
) -> None:
    if (
        _principal_equals(
            principal,
            kind=period.created_principal_kind,
            principal_id=period.created_principal_id,
            user_id=period.created_by_id,
        )
        or _principal_equals(
            principal,
            kind=period.run_principal_kind,
            principal_id=period.run_principal_id,
            user_id=period.run_by_id,
        )
        or _principal_equals(
            principal,
            kind=period.approved_principal_kind,
            principal_id=period.approved_principal_id,
            user_id=period.approved_by_id,
        )
    ):
        raise PermissionException(
            _("The payroll creator, maker, or approver cannot record its payment."),
            code="payroll_self_disbursement",
        )
    if principal.user_id == line.teacher_user_id_snapshot or (
        principal.kind == "teacher" and principal.principal_id == line.teacher_id
    ):
        raise PermissionException(
            _("A teacher cannot record their own salary payment."),
            code="payroll_beneficiary_disbursement",
        )


def _validate_paid_at(value: dt.datetime) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ValidationException(
            fields={"paid_at": [_("Use an ISO-8601 timestamp with an offset.")]},
            code="validation_error",
        )
    normalized = value.astimezone(dt.UTC)
    if normalized > timezone.now().astimezone(dt.UTC) + dt.timedelta(minutes=5):
        raise ValidationException(
            fields={"paid_at": [_("Must not be in the future.")]}, code="validation_error"
        )
    return normalized


def _validate_reference(value: str, *, field: str = "external_reference") -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValidationException(fields={field: [_("This field is required.")]}, code="validation_error")
    if len(normalized) > 128 or "\x00" in normalized:
        raise ValidationException(
            fields={field: [_("Use at most 128 characters without NUL bytes.")]},
            code="validation_error",
        )
    return normalized


def _assert_payment_after_period(*, paid_at: dt.datetime, period: PayrollPeriod) -> None:
    _start, end_exclusive = _period_bounds(period)
    if paid_at < end_exclusive.astimezone(dt.UTC):
        raise ValidationException(
            fields={"paid_at": [_("Must be after the payroll period has ended.")]},
            code="validation_error",
        )
    if period.decided_at is None or paid_at < period.decided_at.astimezone(dt.UTC):
        raise ValidationException(
            fields={"paid_at": [_("Must not predate payroll approval.")]},
            code="validation_error",
        )


def _reconciliation_fingerprint(
    *, label: str, line_id: int, amount: Decimal, payment_method_id: int, reference: str, paid_at
) -> str:
    return _fingerprint(
        label,
        {
            "line_item": line_id,
            "amount_uzs": str(amount),
            "payment_method": payment_method_id,
            "external_reference": reference,
            "paid_at": paid_at.isoformat(),
        },
    )


def _audit_reconciliation(row: PayrollReconciliation, *, actor, principal: RolePrincipal) -> None:
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_log

    audit_log(
        actor=actor,
        action="create",
        resource_type="payroll.PayrollReconciliation",
        resource_id=row.pk,
        after={
            "kind": row.kind,
            "line_item_id": row.line_item_id,
            "branch_id": row.line_item.branch_at_run_id,
            "department_id": row.line_item.department_at_run_id,
        },
        scope=scoped_audit_scope(
            row.line_item.branch_at_run_id,
            row.line_item.department_at_run_id,
        ),
        actor_principal=principal,
    )


@transaction.atomic
def reconcile_payment(
    *,
    period: PayrollPeriod,
    dto: PaymentReconciliationDTO,
    actor,
    principal: RolePrincipal,
) -> PayrollReconciliation:
    from apps.approvals.models import LedgerEntry
    from apps.finance.models import PaymentMethod

    _assert_staff_actor(actor=actor, principal=principal)
    line_item_id = _positive_id(dto.line_item_id, field="line_item")
    payment_method_id = _positive_id(dto.payment_method_id, field="payment_method")
    assert line_item_id is not None
    assert payment_method_id is not None
    raw = validate_idempotency_key(dto.idempotency_key)
    key_hash = _key_hash(principal=principal, raw=raw)
    amount = _money(dto.amount_uzs, field="amount_uzs")
    if amount <= 0:
        raise ValidationException(
            fields={"amount_uzs": [_("Must be greater than zero.")]}, code="validation_error"
        )
    paid_at = _validate_paid_at(dto.paid_at)
    external_reference = _validate_reference(dto.external_reference)
    fingerprint = _reconciliation_fingerprint(
        label="reconciliation-payment",
        line_id=line_item_id,
        amount=amount,
        payment_method_id=payment_method_id,
        reference=external_reference,
        paid_at=paid_at,
    )
    existing = (
        PayrollReconciliation.objects.select_related("line_item")
        .filter(idempotency_key_hash=key_hash)
        .first()
    )
    if existing is not None:
        if existing.operation_fingerprint != fingerprint:
            raise ConflictException(code="idempotency_key_reused")
        return existing

    locked_period = PayrollPeriod.objects.select_for_update().get(pk=period.pk)
    existing = (
        PayrollReconciliation.objects.select_related("line_item", "payment_method")
        .filter(idempotency_key_hash=key_hash)
        .first()
    )
    if existing is not None:
        if existing.operation_fingerprint != fingerprint:
            raise ConflictException(code="idempotency_key_reused")
        return existing
    if locked_period.status not in {
        PayrollPeriod.Status.APPROVED,
        PayrollPeriod.Status.PAYMENT_IN_PROGRESS,
    }:
        raise ConflictException(
            _("Only an approved payroll period can be reconciled."),
            code="payroll_period_not_approved",
        )
    _assert_payment_after_period(paid_at=paid_at, period=locked_period)
    line = (
        PayrollLineItem.objects.select_for_update()
        .select_related("teacher", "period")
        .filter(pk=line_item_id, period=locked_period)
        .first()
    )
    if line is None:
        raise NotFoundException(code="not_found")
    _assert_disburser_separation(period=locked_period, line=line, principal=principal)
    method = PaymentMethod.objects.select_for_update().filter(pk=payment_method_id, is_active=True).first()
    if method is None:
        raise ValidationException(
            fields={"payment_method": [_("Choose an active payment method.")]},
            code="validation_error",
        )
    if PayrollReconciliation.objects.filter(
        payment_method=method, external_reference=external_reference
    ).exists():
        raise ConflictException(
            _("This payment reference has already been reconciled."),
            code="payment_reference_reused",
        )
    current_paid = _line_paid_total(line)
    outstanding = (line.net_amount_uzs - current_paid).quantize(CENT)
    if amount > outstanding:
        raise ConflictException(
            _("The reconciliation exceeds this payslip's outstanding amount."),
            code="payroll_overpayment",
        )
    ledger = LedgerEntry.objects.create(
        direction=LedgerEntry.Direction.OUT,
        entry_type="payroll",
        amount_uzs=amount,
        branch_id=line.branch_at_run_id,
        party_label=line.teacher_name_snapshot[:200],
        payment_method=method,
        source_kind="payroll_line_item",
        source_id=line.pk,
        note=f"Payroll {locked_period.period_start}:{locked_period.period_end}"[:255],
        created_by=actor,
    )
    row = PayrollReconciliation.objects.create(
        line_item=line,
        kind=PayrollReconciliation.Kind.PAYMENT,
        amount_uzs=amount,
        currency=line.currency,
        payment_method=method,
        external_reference=external_reference,
        paid_at=paid_at,
        ledger_entry=ledger,
        recorded_by=actor,
        recorded_principal_kind=principal.kind,
        recorded_principal_id=principal.principal_id,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    previous_status = locked_period.status
    locked_period.paid_total_uzs = (locked_period.paid_total_uzs + amount).quantize(CENT)
    locked_period.status = (
        PayrollPeriod.Status.PAID
        if locked_period.paid_total_uzs == locked_period.net_total_uzs
        else PayrollPeriod.Status.PAYMENT_IN_PROGRESS
    )
    locked_period.save(update_fields=("paid_total_uzs", "status", "updated_at"))
    PayrollPeriodEvent.objects.create(
        period=locked_period,
        action=PayrollPeriodEvent.Action.PAYMENT,
        actor=actor,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        note=f"reconciliation:{row.pk}",
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    _audit_reconciliation(row, actor=actor, principal=principal)
    if previous_status != locked_period.status:
        _audit_period(
            locked_period,
            actor=actor,
            principal=principal,
            action="update",
            previous_status=previous_status,
        )
    return row


@transaction.atomic
def reverse_payment(
    *,
    reconciliation: PayrollReconciliation,
    dto: ReversalDTO,
    actor,
    principal: RolePrincipal,
) -> PayrollReconciliation:
    from apps.approvals.models import LedgerEntry
    from apps.finance.models import PaymentMethod

    _assert_staff_actor(actor=actor, principal=principal)
    raw = validate_idempotency_key(dto.idempotency_key)
    key_hash = _key_hash(principal=principal, raw=raw)
    paid_at = _validate_paid_at(dto.paid_at)
    external_reference = _validate_reference(dto.external_reference)
    reason = dto.reason.strip() if isinstance(dto.reason, str) else ""
    if not reason:
        raise ValidationException(
            fields={"reason": [_("A reversal reason is required.")]}, code="validation_error"
        )
    if len(reason) > 255 or "\x00" in reason:
        raise ValidationException(
            fields={"reason": [_("Use at most 255 characters without NUL bytes.")]},
            code="validation_error",
        )
    fingerprint = _fingerprint(
        "reconciliation-reversal",
        {
            "reconciliation": reconciliation.pk,
            "external_reference": external_reference,
            "paid_at": paid_at.isoformat(),
            "reason": reason,
        },
    )
    existing = (
        PayrollReconciliation.objects.select_related("line_item")
        .filter(idempotency_key_hash=key_hash)
        .first()
    )
    if existing is not None:
        if existing.operation_fingerprint != fingerprint:
            raise ConflictException(code="idempotency_key_reused")
        return existing

    original = (
        PayrollReconciliation.objects.select_for_update()
        .select_related("line_item", "line_item__period", "payment_method")
        .get(pk=reconciliation.pk)
    )
    period = PayrollPeriod.objects.select_for_update().get(pk=original.line_item.period_id)
    line = PayrollLineItem.objects.select_for_update().get(pk=original.line_item_id)
    existing = (
        PayrollReconciliation.objects.select_related("line_item", "payment_method")
        .filter(idempotency_key_hash=key_hash)
        .first()
    )
    if existing is not None:
        if existing.operation_fingerprint != fingerprint:
            raise ConflictException(code="idempotency_key_reused")
        return existing
    if original.kind != PayrollReconciliation.Kind.PAYMENT:
        raise ConflictException(_("Only a payment can be reversed."), code="payroll_reversal_invalid")
    if PayrollReconciliation.objects.filter(reverses=original).exists():
        raise ConflictException(
            _("This payment has already been reversed."), code="payroll_payment_already_reversed"
        )
    _assert_disburser_separation(period=period, line=line, principal=principal)
    _assert_payment_after_period(paid_at=paid_at, period=period)
    if paid_at < original.paid_at.astimezone(dt.UTC):
        raise ValidationException(
            fields={"paid_at": [_("Must not predate the original payment.")]},
            code="validation_error",
        )
    method = PaymentMethod.objects.select_for_update().get(pk=original.payment_method_id)
    if PayrollReconciliation.objects.filter(
        payment_method=method, external_reference=external_reference
    ).exists():
        raise ConflictException(code="payment_reference_reused")
    ledger = LedgerEntry.objects.create(
        direction=LedgerEntry.Direction.IN,
        entry_type="payroll_reversal",
        amount_uzs=original.amount_uzs,
        branch_id=line.branch_at_run_id,
        party_label=line.teacher_name_snapshot[:200],
        payment_method=method,
        source_kind="payroll_reconciliation",
        source_id=original.pk,
        note=reason,
        created_by=actor,
    )
    reversal = PayrollReconciliation.objects.create(
        line_item=line,
        kind=PayrollReconciliation.Kind.REVERSAL,
        reverses=original,
        amount_uzs=original.amount_uzs,
        currency=original.currency,
        payment_method=method,
        external_reference=external_reference,
        paid_at=paid_at,
        reason=reason,
        ledger_entry=ledger,
        recorded_by=actor,
        recorded_principal_kind=principal.kind,
        recorded_principal_id=principal.principal_id,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    previous_status = period.status
    period.paid_total_uzs = (period.paid_total_uzs - original.amount_uzs).quantize(CENT)
    if period.paid_total_uzs < 0:
        raise ConflictException(code="payroll_reversal_invalid")
    period.status = (
        PayrollPeriod.Status.APPROVED
        if period.paid_total_uzs == 0
        else PayrollPeriod.Status.PAYMENT_IN_PROGRESS
    )
    period.save(update_fields=("paid_total_uzs", "status", "updated_at"))
    PayrollPeriodEvent.objects.create(
        period=period,
        action=PayrollPeriodEvent.Action.REVERSAL,
        actor=actor,
        actor_principal_kind=principal.kind,
        actor_principal_id=principal.principal_id,
        note=f"reversal:{reversal.pk}",
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    _audit_reconciliation(reversal, actor=actor, principal=principal)
    _audit_period(
        period,
        actor=actor,
        principal=principal,
        action="update",
        previous_status=previous_status,
    )
    return reversal


def _export_fingerprint(*, period_id: int, dto: ExportCreateDTO) -> str:
    return _fingerprint(
        "export",
        {
            "period": period_id,
            "format": dto.format,
            "teacher": dto.teacher_id,
            "payment_state": dto.payment_state,
        },
    )


@transaction.atomic
def request_export(
    *,
    period: PayrollPeriod,
    dto: ExportCreateDTO,
    actor,
    principal: RolePrincipal,
) -> PayrollExport:
    from core.job_limits import lock_tenant_job_queue

    _assert_staff_actor(actor=actor, principal=principal)
    raw = validate_idempotency_key(dto.idempotency_key)
    if dto.format not in PayrollExport.Format.values:
        raise ValidationException(fields={"format": [_("Choose xlsx or pdf.")]}, code="validation_error")
    if dto.payment_state not in {None, "unpaid", "partial", "paid"}:
        raise ValidationException(
            fields={"payment_state": [_("Choose unpaid, partial, or paid.")]},
            code="validation_error",
        )
    if dto.teacher_id is not None and (
        isinstance(dto.teacher_id, bool) or not isinstance(dto.teacher_id, int) or dto.teacher_id <= 0
    ):
        raise ValidationException(
            fields={"teacher": [_("Choose a positive teacher ID.")]},
            code="validation_error",
        )
    key_hash = _key_hash(principal=principal, raw=raw)
    fingerprint = _export_fingerprint(period_id=period.pk, dto=dto)
    existing = PayrollExport.objects.filter(idempotency_key_hash=key_hash).first()
    if existing is not None:
        if existing.operation_fingerprint != fingerprint:
            raise ConflictException(code="idempotency_key_reused")
        return existing
    locked_period = PayrollPeriod.objects.select_for_update().get(pk=period.pk)
    if locked_period.status == PayrollPeriod.Status.DRAFT:
        raise ConflictException(
            _("Run payroll before requesting an export."), code="payroll_period_not_frozen"
        )
    if (
        dto.teacher_id is not None
        and not PayrollLineItem.objects.filter(period=locked_period, teacher_id=dto.teacher_id).exists()
    ):
        raise NotFoundException(code="not_found")
    lock_tenant_job_queue("payroll-export-admission")
    # Admission and the period lock serialize equivalent requests.  Resolve a
    # key again after waiting so a concurrent retry returns the first job
    # instead of leaking a unique-constraint error.
    existing = PayrollExport.objects.filter(idempotency_key_hash=key_hash).first()
    if existing is not None:
        if existing.operation_fingerprint != fingerprint:
            raise ConflictException(code="idempotency_key_reused")
        return existing
    active = PayrollExport.objects.filter(
        status__in=(PayrollExport.Status.QUEUED, PayrollExport.Status.RUNNING)
    ).count()
    if active >= MAX_ACTIVE_EXPORTS:
        from core.exceptions import ThrottledException

        raise ThrottledException(
            _("Too many payroll exports are already in progress."),
            code="payroll_export_queue_full",
            wait=30,
        )
    export = PayrollExport.objects.create(
        period=locked_period,
        format=dto.format,
        filters={
            **({"teacher": dto.teacher_id} if dto.teacher_id is not None else {}),
            **({"payment_state": dto.payment_state} if dto.payment_state is not None else {}),
        },
        requested_by=actor,
        requested_principal_kind=principal.kind,
        requested_principal_id=principal.principal_id,
        idempotency_key_hash=key_hash,
        operation_fingerprint=fingerprint,
    )
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_log

    audit_log(
        actor=actor,
        action="export",
        resource_type="payroll.PayrollExport",
        resource_id=export.pk,
        after={
            "period_id": locked_period.pk,
            "format": dto.format,
            "branch_id": locked_period.branch_id,
            "department_id": locked_period.department_id,
        },
        scope=scoped_audit_scope(locked_period.branch_id, locked_period.department_id),
        actor_principal=principal,
    )
    schema = current_schema()
    transaction.on_commit(lambda: _enqueue_export(export.pk, schema))
    return export


def _enqueue_export(export_id: int, schema: str) -> None:
    from celery_tasks.payroll_tasks import build_payroll_export

    build_payroll_export.delay(export_id, _schema_name=schema)


def _export_authorized(export: PayrollExport) -> bool:
    user = export.requested_by
    if user is None or not user.is_active:
        return False
    if user.is_superuser:
        return True
    roles = get_user_roles_for_user(
        user,
        principal_kind=export.requested_principal_kind,
        principal_id=export.requested_principal_id,
        principal_validated=False,
    )
    if not has_permission_code(roles, "compensation:read"):
        return False
    return _scope_allowed(
        roles=roles,
        permission="compensation:read",
        branch_id=export.period.branch_id,
        department_id=export.period.department_id,
    )


def _export_rows(export: PayrollExport) -> list[dict[str, Any]]:
    queryset = (
        PayrollLineItem.objects.filter(period=export.period)
        .select_related("payslip", "branch_at_run", "department_at_run")
        .prefetch_related("reconciliations")
        .order_by("teacher_name_snapshot", "id")
    )
    teacher_id = export.filters.get("teacher")
    if teacher_id is not None:
        queryset = queryset.filter(teacher_id=teacher_id)
    payment_state = export.filters.get("payment_state")
    rows: list[dict[str, Any]] = []
    for line in queryset[: MAX_PAYROLL_TEACHERS + 1]:
        paid = sum(
            (
                (
                    reconciliation.amount_uzs
                    if reconciliation.kind == PayrollReconciliation.Kind.PAYMENT
                    else -reconciliation.amount_uzs
                )
                for reconciliation in line.reconciliations.all()
            ),
            Decimal("0"),
        )
        state = "unpaid" if paid == 0 else "paid" if paid == line.net_amount_uzs else "partial"
        if payment_state and state != payment_state:
            continue
        rows.append(
            {
                "payslip": line.payslip.document_number,
                "teacher_code": line.teacher_code_snapshot,
                "teacher_name": line.teacher_name_snapshot,
                "branch": line.branch_at_run.name,
                "department": line.department_at_run.name if line.department_at_run else "",
                "method": line.payout_method_snapshot,
                "base_amount_uzs": str(line.base_amount_uzs),
                "bonus_amount_uzs": str(line.bonus_amount_uzs),
                "deduction_amount_uzs": str(line.deduction_amount_uzs),
                "net_amount_uzs": str(line.net_amount_uzs),
                "paid_amount_uzs": str(paid.quantize(CENT)),
                "outstanding_amount_uzs": str((line.net_amount_uzs - paid).quantize(CENT)),
                "currency": line.currency,
                "payment_state": state,
            }
        )
    if len(rows) > MAX_PAYROLL_TEACHERS:
        raise UnprocessableEntity(code="payroll_export_too_large")
    return rows


_EXPORT_COLUMNS = (
    "payslip",
    "teacher_code",
    "teacher_name",
    "branch",
    "department",
    "method",
    "base_amount_uzs",
    "bonus_amount_uzs",
    "deduction_amount_uzs",
    "net_amount_uzs",
    "paid_amount_uzs",
    "outstanding_amount_uzs",
    "currency",
    "payment_state",
)


def _render_xlsx(rows: list[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook

    from core.spreadsheets import safe_cell

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Payroll")
    sheet.append([safe_cell(column) for column in _EXPORT_COLUMNS])
    for row in rows:
        sheet.append([safe_cell(row[column]) for column in _EXPORT_COLUMNS])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _render_pdf(export: PayrollExport, rows: list[dict[str, Any]]) -> bytes:
    from weasyprint import HTML

    headers = "".join(f"<th>{escape(column)}</th>" for column in _EXPORT_COLUMNS)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(row[column]))}</td>" for column in _EXPORT_COLUMNS) + "</tr>"
        for row in rows
    )
    period = export.period
    html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
    @page {{ size: A4 landscape; margin: 12mm; }}
    body {{ font: 9px sans-serif; color: #111; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #bbb; padding: 3px; text-align: left; }}
    th {{ background: #eee; }}
    </style></head><body>
    <h1>Payroll {escape(period.label)}</h1>
    <p>{period.period_start.isoformat()} – {period.period_end.isoformat()}</p>
    <table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>
    </body></html>"""
    return HTML(string=html).write_pdf()


def expected_export_key(export: PayrollExport) -> str:
    if export.format not in PayrollExport.Format.values:
        return ""
    return f"{current_schema()}/payroll/exports/{export.pk}.{export.format}"


def _audit_export_status(export: PayrollExport, *, action: str, previous_status: str) -> None:
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_log

    actor = export.requested_by
    principal = (
        RolePrincipal(
            kind=export.requested_principal_kind,
            principal_id=export.requested_principal_id,
            user_id=actor.pk,
        )
        if actor is not None
        else None
    )
    audit_log(
        actor=actor,
        actor_principal=principal,
        action=action,
        resource_type="payroll.PayrollExport",
        resource_id=export.pk,
        before={
            "status": previous_status,
            "branch_id": export.period.branch_id,
            "department_id": export.period.department_id,
        },
        after={
            "status": export.status,
            "branch_id": export.period.branch_id,
            "department_id": export.period.department_id,
            "file_bytes": export.file_bytes,
            "error_code": export.error_code,
        },
        scope=scoped_audit_scope(
            export.period.branch_id,
            export.period.department_id,
        ),
    )


def build_export(export_id: int) -> str | None:
    from core.exceptions import ConflictException
    from core.job_limits import release_job_execution, try_acquire_job_execution

    if not try_acquire_job_execution("payroll-export", export_id):
        raise ConflictException(code="payroll_export_in_progress")
    try:
        with transaction.atomic():
            export = (
                PayrollExport.objects.select_for_update()
                .select_related("period", "requested_by")
                .get(pk=export_id)
            )
            if export.status in {PayrollExport.Status.DONE, PayrollExport.Status.FAILED}:
                return export.s3_key if export.s3_key == expected_export_key(export) else None
            if not _export_authorized(export):
                raise PermissionException(code="payroll_export_forbidden")
            export.status = PayrollExport.Status.RUNNING
            export.started_at = timezone.now()
            export.error_code = ""
            export.save(update_fields=("status", "started_at", "error_code"))

        export = PayrollExport.objects.select_related("period", "requested_by").get(pk=export_id)
        rows = _export_rows(export)
        payload = (
            _render_xlsx(rows) if export.format == PayrollExport.Format.XLSX else _render_pdf(export, rows)
        )

        # A queued export may outlive the grant that authorized it.  Re-check
        # immediately before publishing the artifact.
        export.refresh_from_db()
        export = PayrollExport.objects.select_related("period", "requested_by").get(pk=export_id)
        if not _export_authorized(export):
            raise PermissionException(code="payroll_export_forbidden")
        from infrastructure.storage.s3_client import upload_bytes

        key = expected_export_key(export)
        content_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if export.format == PayrollExport.Format.XLSX
            else "application/pdf"
        )
        upload_bytes(key, payload, content_type=content_type)
        with transaction.atomic():
            locked = (
                PayrollExport.objects.select_for_update()
                .select_related("period", "requested_by")
                .get(pk=export_id)
            )
            previous_status = locked.status
            locked.status = PayrollExport.Status.DONE
            locked.s3_key = key
            locked.file_bytes = len(payload)
            locked.finished_at = timezone.now()
            locked.save(update_fields=("status", "s3_key", "file_bytes", "finished_at"))
            _audit_export_status(
                locked,
                action="export.complete",
                previous_status=previous_status,
            )
        return key
    finally:
        release_job_execution("payroll-export", export_id)


def reset_export_for_retry(export_id: int) -> None:
    PayrollExport.objects.filter(pk=export_id).exclude(status=PayrollExport.Status.DONE).update(
        status=PayrollExport.Status.QUEUED,
        started_at=None,
    )


def mark_export_failed(export_id: int, exc: Exception) -> None:
    logger.error(
        "Payroll export %s failed (%s)",
        export_id,
        type(exc).__name__,
    )
    with transaction.atomic():
        export = (
            PayrollExport.objects.select_for_update()
            .select_related("period", "requested_by")
            .filter(pk=export_id)
            .exclude(status=PayrollExport.Status.DONE)
            .first()
        )
        if export is None:
            return
        previous_status = export.status
        export.status = PayrollExport.Status.FAILED
        export.error_code = "payroll_export_failed"
        export.finished_at = timezone.now()
        export.save(update_fields=("status", "error_code", "finished_at"))
        _audit_export_status(
            export,
            action="export.failed",
            previous_status=previous_status,
        )


def presign_export(export: PayrollExport) -> str | None:
    key = expected_export_key(export)
    if export.status != PayrollExport.Status.DONE or export.s3_key != key:
        return None
    from infrastructure.storage.s3_client import presign_download

    return presign_download(
        key,
        expires_in=DOWNLOAD_TTL_SECONDS,
        download_filename=f"payroll-{export.period_id}.{export.format}",
        response_content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if export.format == PayrollExport.Format.XLSX
            else "application/pdf"
        ),
    )
