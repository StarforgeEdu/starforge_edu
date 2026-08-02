"""Permission-pruned read model for one student's leadership profile.

The profile is assembled from scoped domain selectors rather than bypassing
their authorization rules.  Every aggregate is bounded by one student and an
inclusive organization-time window; absent authority omits a section instead
of returning a misleading zero.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Any

from django.db.models import (
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.students.dto.student_dto import (
    LeadershipProfileAccessDTO,
    LeadershipProfileWindowDTO,
)
from apps.students.models import StudentProfile

_ZERO = Decimal("0.00")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _window_bounds(window: LeadershipProfileWindowDTO) -> tuple[datetime, datetime]:
    current_tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(window.date_from, time.min), current_tz),
        timezone.make_aware(datetime.combine(window.date_to, time.max), current_tz),
    )


def _money(value: Decimal | int | None, *, currency: str = "UZS") -> dict[str, Any]:
    amount = Decimal(value or _ZERO).quantize(Decimal("0.01"))
    return {
        # Compatibility with the existing decimal-major v1 finance contract.
        "amount_uzs": format(amount, ".2f"),
        # Additive, explicit minor-unit contract for new management consumers.
        "amount_minor": int(amount * 100),
        "currency": currency,
    }


def _coverage_entry(*, available: bool, window: LeadershipProfileWindowDTO | None = None) -> dict:
    payload: dict[str, Any] = {"status": "available" if available else "not_authorized"}
    if available and window is not None:
        payload["window"] = {
            "date_from": window.date_from.isoformat(),
            "date_to": window.date_to.isoformat(),
            "inclusive": True,
        }
    return payload


def _identity(student: StudentProfile) -> dict[str, Any]:
    cohort = student.current_cohort if student.current_cohort_id else None
    return {
        "id": student.pk,
        "public_student_id": student.student_id,
        "username": student.username,
        "full_name": student.get_full_name(),
        "first_name": student.first_name,
        "middle_name": student.middle_name,
        "last_name": student.last_name,
        "phone": student.phone,
        "email": student.email,
        "birthdate": _iso(student.birthdate),
        "gender": student.gender,
        "status": student.status,
        "is_active": student.is_active,
        "branch": {
            "id": student.branch_id,
            "name": student.branch.name,
        },
        "current_group": (
            {
                "id": cohort.pk,
                "name": cohort.name,
                "level": cohort.level,
                "department": (
                    {"id": cohort.department_id, "name": cohort.department.name}
                    if cohort.department_id and cohort.department is not None
                    else None
                ),
            }
            if cohort is not None
            else None
        ),
        "academic_level": student.academic_level,
        "location": student.location,
        "previous_school": student.previous_school,
        "enrollment_date": _iso(student.enrollment_date),
        "block": {
            "is_blocked": student.is_blocked,
            "blocked_at": _iso(student.blocked_at),
            "reason": student.block_reason,
        },
        # Existing keys are not exposed until tenant/student ownership can be
        # proven.  The boolean lets clients render a deliberate unavailable state.
        "photo": {
            "available": bool(student.photo),
            "download_url": None,
        },
    }


def _teachers(student: StudentProfile) -> list[dict[str, Any]]:
    cohort = student.current_cohort if student.current_cohort_id else None
    if cohort is None:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    if cohort.primary_teacher_id and cohort.primary_teacher is not None:
        teacher = cohort.primary_teacher
        seen.add(teacher.pk)
        rows.append(
            {
                "id": teacher.pk,
                "name": teacher.get_full_name(),
                "responsibility": "primary",
            }
        )

    from apps.cohorts.models import CohortTeacher

    additional = CohortTeacher.objects.filter(cohort_id=cohort.pk).select_related(
        "teacher",
        "teacher_type",
    )
    for assignment in additional:
        if assignment.teacher_id in seen:
            continue
        seen.add(assignment.teacher_id)
        rows.append(
            {
                "id": assignment.teacher_id,
                "name": assignment.teacher.get_full_name(),
                "responsibility": (
                    assignment.teacher_type.name
                    if assignment.teacher_type_id and assignment.teacher_type is not None
                    else assignment.role
                ),
            }
        )
    return rows


def _learning(
    *,
    student: StudentProfile,
    user: Any,
    roles: set[str],
    window: LeadershipProfileWindowDTO,
    include_teachers: bool,
) -> dict[str, Any]:
    from apps.academics.models import ExamResult
    from apps.academics.selectors import scoped_exams, scoped_grades, scoped_transcripts
    from apps.assignments.models import Assignment
    from apps.assignments.selectors import scoped_assignments, scoped_submissions

    grades = list(
        scoped_grades(user=user, roles=roles)
        .filter(
            student_id=student.pk,
            is_published=True,
            is_valid=True,
            term__start_date__lte=window.date_to,
            term__end_date__gte=window.date_from,
        )
        .select_related("subject", "term")
        .order_by("-term__end_date", "subject__name")[:10]
    )

    cohort_id = student.current_cohort_id
    visible_exams = scoped_exams(user=user, roles=roles).filter(
        is_published=True,
        requires_republish=False,
        exam_date__range=(window.date_from, window.date_to),
    )
    if cohort_id is not None:
        visible_exams = visible_exams.filter(cohort_id=cohort_id)
    else:
        visible_exams = visible_exams.none()
    results = list(
        ExamResult.objects.filter(
            student_id=student.pk,
            exam_id__in=Subquery(visible_exams.order_by().values("pk")),
        )
        .select_related("exam__subject", "exam__term")
        .order_by("-exam__exam_date", "-pk")[:10]
    )

    assignment_summary = {
        "assigned": 0,
        "completed": 0,
        "open": 0,
        "late": 0,
    }
    if cohort_id is not None:
        starts_at, ends_at = _window_bounds(window)
        assignments = scoped_assignments(user=user, roles=roles).filter(
            cohort_id=cohort_id,
            status__in=(Assignment.Status.PUBLISHED, Assignment.Status.CLOSED),
            due_at__gte=starts_at,
            due_at__lte=ends_at,
        )
        assigned_ids = assignments.order_by().values("pk")
        assigned = assignments.count()
        submissions = scoped_submissions(user=user, roles=roles).filter(
            student_id=student.pk,
            assignment_id__in=Subquery(assigned_ids),
        )
        submitted = submissions.aggregate(
            completed=Count("assignment_id", distinct=True),
            late=Count("assignment_id", filter=Q(is_late=True), distinct=True),
        )
        completed = int(submitted["completed"] or 0)
        assignment_summary = {
            "assigned": assigned,
            "completed": completed,
            "open": max(assigned - completed, 0),
            "late": int(submitted["late"] or 0),
        }

    latest_transcript = (
        scoped_transcripts(user=user, roles=roles)
        .filter(student_id=student.pk)
        .order_by("-created_at")
        .first()
    )

    subject_rows: dict[int, dict[str, Any]] = {}
    for grade in grades:
        subject_rows[grade.subject_id] = {
            "id": grade.subject_id,
            "code": grade.subject.code,
            "name": grade.subject.name,
        }
    for result in results:
        subject = result.exam.subject
        subject_rows[subject.pk] = {
            "id": subject.pk,
            "code": subject.code,
            "name": subject.name,
        }

    return {
        "teachers": _teachers(student) if include_teachers else None,
        "subjects": sorted(subject_rows.values(), key=lambda item: (item["name"], item["id"])),
        "recent_grades": [
            {
                "id": grade.pk,
                "subject": {"id": grade.subject_id, "name": grade.subject.name},
                "term": {"id": grade.term_id, "name": grade.term.name},
                "value_raw_pct": float(grade.value_raw),
                "value_display": grade.value_display,
                "published_at": _iso(grade.published_at),
                "computed_at": _iso(grade.computed_at),
            }
            for grade in grades
        ],
        "recent_exam_results": [
            {
                "id": result.pk,
                "exam": {
                    "id": result.exam_id,
                    "title": result.exam.title,
                    "date": result.exam.exam_date.isoformat(),
                },
                "subject": {
                    "id": result.exam.subject_id,
                    "name": result.exam.subject.name,
                },
                "score": format(result.score, ".2f"),
                "maximum": format(result.exam.max_score, ".2f"),
                "score_fraction": round(float(result.score / result.exam.max_score), 4),
                "last_graded_at": _iso(result.graded_at),
            }
            for result in results
        ],
        "assignments": assignment_summary,
        "latest_transcript": (
            {
                "id": latest_transcript.pk,
                "term": latest_transcript.term_id,
                "status": latest_transcript.status,
                "generated_at": _iso(latest_transcript.generated_at),
                "requested_at": _iso(latest_transcript.created_at),
            }
            if latest_transcript is not None
            else None
        ),
    }


def _attendance(
    *,
    student: StudentProfile,
    user: Any,
    roles: set[str],
    window: LeadershipProfileWindowDTO,
) -> dict[str, Any]:
    from apps.attendance.models import AttendanceRecord
    from apps.attendance.selectors import scoped_records

    starts_at, ends_at = _window_bounds(window)
    records = scoped_records(user=user, roles=roles).filter(
        student_id=student.pk,
        lesson__starts_at__gte=starts_at,
        lesson__starts_at__lte=ends_at,
    )
    statuses = AttendanceRecord.Status
    counts = records.aggregate(
        present=Count("pk", filter=Q(status=statuses.PRESENT)),
        late=Count("pk", filter=Q(status=statuses.LATE)),
        absent=Count("pk", filter=Q(status=statuses.ABSENT)),
        excused=Count("pk", filter=Q(status=statuses.EXCUSED)),
    )
    attended = int(counts["present"] or 0) + int(counts["late"] or 0)
    denominator = attended + int(counts["absent"] or 0)

    latest = records.select_related("lesson__cohort").order_by("-lesson__starts_at", "-pk").first()
    latest_absence = (
        records.filter(status=statuses.ABSENT)
        .order_by("-lesson__starts_at", "-pk")
        .values("lesson__starts_at", "pk")
        .first()
    )
    streak = records.filter(status__in=(statuses.PRESENT, statuses.LATE))
    if latest_absence is not None:
        streak = streak.filter(
            Q(lesson__starts_at__gt=latest_absence["lesson__starts_at"])
            | Q(
                lesson__starts_at=latest_absence["lesson__starts_at"],
                pk__gt=latest_absence["pk"],
            )
        )

    per_group = []
    for row in (
        records.order_by()
        .values("lesson__cohort_id", "lesson__cohort__name")
        .annotate(
            present=Count("pk", filter=Q(status=statuses.PRESENT)),
            late=Count("pk", filter=Q(status=statuses.LATE)),
            absent=Count("pk", filter=Q(status=statuses.ABSENT)),
            excused=Count("pk", filter=Q(status=statuses.EXCUSED)),
        )
        .order_by("lesson__cohort__name", "lesson__cohort_id")
    ):
        group_attended = int(row["present"] or 0) + int(row["late"] or 0)
        group_denominator = group_attended + int(row["absent"] or 0)
        per_group.append(
            {
                "group": {
                    "id": row["lesson__cohort_id"],
                    "name": row["lesson__cohort__name"],
                },
                "attended": group_attended,
                "countable_sessions": group_denominator,
                "attendance_rate_fraction": (
                    round(group_attended / group_denominator, 4) if group_denominator else None
                ),
                "excused": int(row["excused"] or 0),
            }
        )

    return {
        "metric_definition": "(present + late) / (present + late + absent); excused is excluded",
        "present": int(counts["present"] or 0),
        "late": int(counts["late"] or 0),
        "absent": int(counts["absent"] or 0),
        "excused": int(counts["excused"] or 0),
        "attended": attended,
        "countable_sessions": denominator,
        "attendance_rate_fraction": round(attended / denominator, 4) if denominator else None,
        "current_attendance_streak": streak.count(),
        "last_attendance": (
            {
                "lesson": latest.lesson_id,
                "group": latest.lesson.cohort_id,
                "group_name": latest.lesson.cohort.name,
                "starts_at": _iso(latest.lesson.starts_at),
                "status": latest.status,
            }
            if latest is not None
            else None
        ),
        "per_group": per_group,
    }


def _family(*, student: StudentProfile, safeguarding: bool) -> dict[str, Any]:
    from apps.parents.models import Guardian, PickupAuthorization

    guardians = Guardian.objects.filter(
        student_id=student.pk,
        revoked_at__isnull=True,
    ).select_related("parent")
    if not safeguarding:
        guardians = guardians.defer("custody_notes", "parent__notes")
    guardian_rows = [
        {
            "id": guardian.pk,
            "parent": guardian.parent_id,
            "name": guardian.parent.get_full_name(),
            "relationship": guardian.relationship,
            "is_primary": guardian.is_primary,
            "contacts": {
                "phone": guardian.parent.phone or None,
                "email": guardian.parent.email or None,
                "verification_status": "not_recorded",
            },
            **({"custody_notes": guardian.custody_notes} if safeguarding else {}),
        }
        for guardian in guardians
    ]
    pickups = [
        {
            "id": pickup.pk,
            "name": pickup.full_name,
            "phone": pickup.phone,
            "relationship": pickup.relationship,
        }
        for pickup in PickupAuthorization.objects.filter(
            student_id=student.pk,
            is_active=True,
        ).order_by("full_name", "pk")
    ]
    payload: dict[str, Any] = {
        "guardians": guardian_rows,
        "pickup_authorizations": pickups,
        "consent_flags": None,
    }
    if safeguarding:
        student.refresh_from_db(fields=("medical_notes", "emergency_contacts"))
        payload["safeguarding"] = {
            "medical_notes": student.medical_notes,
            "emergency_contacts": student.emergency_contacts,
        }
    return payload


def _finance(
    *,
    student: StudentProfile,
    user: Any,
    roles: set[str],
    window: LeadershipProfileWindowDTO,
) -> dict[str, Any]:
    from apps.finance.models import Discount, FeeSchedule, Invoice, PaymentAllocation, Refund
    from apps.finance.selectors import scoped_invoice_summaries
    from apps.payments.models import Payment

    visible = scoped_invoice_summaries(user=user, roles=roles).filter(student_id=student.pk)
    visible_ids = visible.order_by().values("pk")
    money_field = DecimalField(max_digits=24, decimal_places=2)
    allocation_total = (
        PaymentAllocation.objects.filter(invoice_id=OuterRef("pk"))
        .values("invoice_id")
        .annotate(total=Sum("amount_uzs"))
        .values("total")[:1]
    )
    invoices = Invoice.objects.filter(pk__in=Subquery(visible_ids)).annotate(
        leadership_allocated_uzs=Coalesce(
            Subquery(allocation_total, output_field=money_field),
            Value(_ZERO, output_field=money_field),
            output_field=money_field,
        )
    )
    billed_statuses = (
        Invoice.Status.ISSUED,
        Invoice.Status.PARTIALLY_PAID,
        Invoice.Status.PAID,
        Invoice.Status.OVERDUE,
    )
    open_statuses = (
        Invoice.Status.ISSUED,
        Invoice.Status.PARTIALLY_PAID,
        Invoice.Status.OVERDUE,
    )
    outstanding_expression = Greatest(
        ExpressionWrapper(
            F("total_uzs") - F("leadership_allocated_uzs"),
            output_field=money_field,
        ),
        Value(_ZERO, output_field=money_field),
        output_field=money_field,
    )
    invoice_metrics = invoices.aggregate(
        billed=Sum(
            "total_uzs",
            filter=Q(
                status__in=billed_statuses,
                issue_date__range=(window.date_from, window.date_to),
            ),
        ),
        outstanding=Sum(outstanding_expression, filter=Q(status__in=open_statuses)),
        overdue=Sum(outstanding_expression, filter=Q(status=Invoice.Status.OVERDUE)),
        open_count=Count("pk", filter=Q(status__in=open_statuses)),
        overdue_count=Count("pk", filter=Q(status=Invoice.Status.OVERDUE)),
    )

    starts_at, ends_at = _window_bounds(window)
    allocations = PaymentAllocation.objects.filter(invoice_id__in=Subquery(visible_ids))
    paid = (
        allocations.filter(created_at__gte=starts_at, created_at__lte=ends_at).aggregate(
            total=Sum("amount_uzs")
        )["total"]
        or _ZERO
    )
    refunded = (
        Refund.objects.filter(
            invoice_id__in=Subquery(visible_ids),
            state=Refund.State.COMPLETED,
            updated_at__gte=starts_at,
            updated_at__lte=ends_at,
        ).aggregate(total=Sum("amount_uzs"))["total"]
        or _ZERO
    )

    latest_allocation = allocations.select_related("invoice").order_by("-created_at", "-pk").first()
    latest_payment = None
    if latest_allocation is not None:
        payment = (
            Payment.objects.filter(pk=latest_allocation.payment_id)
            .only(
                "id",
                "provider",
                "status",
                "paid_at",
            )
            .first()
        )
        latest_payment = {
            "payment": latest_allocation.payment_id,
            "allocated": _money(latest_allocation.amount_uzs),
            "provider": payment.provider if payment is not None else None,
            "status": payment.status if payment is not None else "unavailable",
            "paid_at": _iso(payment.paid_at) if payment is not None else None,
        }

    today = timezone.localdate()
    discounts = [
        {
            "id": discount.pk,
            "type": discount.discount_type,
            "percent_pct": float(discount.percent) if discount.percent is not None else None,
            "fixed_amount": (
                _money(discount.fixed_amount_uzs) if discount.fixed_amount_uzs is not None else None
            ),
            "valid_from": _iso(discount.valid_from),
            "valid_until": _iso(discount.valid_until),
        }
        for discount in Discount.objects.filter(
            student_id=student.pk,
            is_active=True,
        )
        .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=today))
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=today))
        .order_by("valid_until", "pk")
    ]
    schedule_filter = Q(cohort_id=student.current_cohort_id) if student.current_cohort_id else Q(pk__in=[])
    schedules = [
        {
            "id": schedule.pk,
            "name": schedule.name,
            "group": schedule.cohort_id,
            "billing_period": schedule.billing_period,
            "amount": _money(schedule.amount_uzs),
            "due_day_of_month": schedule.due_day_of_month,
        }
        for schedule in FeeSchedule.objects.filter(is_active=True)
        .filter(Q(cohort__isnull=True) | schedule_filter)
        .order_by("cohort_id", "name", "pk")
    ]

    return {
        "window": {
            "billed": _money(invoice_metrics["billed"]),
            "collected": _money(paid),
            "refunded": _money(refunded),
        },
        "all_time": {
            "outstanding": _money(invoice_metrics["outstanding"]),
            "overdue": _money(invoice_metrics["overdue"]),
            "open_invoice_count": int(invoice_metrics["open_count"] or 0),
            "overdue_invoice_count": int(invoice_metrics["overdue_count"] or 0),
        },
        "fee_schedules": schedules,
        "discounts": discounts,
        "last_payment": latest_payment,
    }


def build_student_leadership_profile(
    *,
    student: StudentProfile,
    user: Any,
    roles: set[str],
    window: LeadershipProfileWindowDTO,
    access: LeadershipProfileAccessDTO,
) -> dict[str, Any]:
    """Build one consistent, permission-pruned leadership read model."""

    generated_at = timezone.now()
    payload: dict[str, Any] = {
        "generated_at": generated_at.isoformat(),
        "window": {
            "date_from": window.date_from.isoformat(),
            "date_to": window.date_to.isoformat(),
            "inclusive": True,
            "timezone": str(timezone.get_current_timezone()),
        },
        "identity": _identity(student),
        "record_metadata": {
            "created_at": student.created_at.isoformat(),
            "updated_at": student.updated_at.isoformat(),
            "created_by": None,
            "updated_by": None,
            "custom_fields": None,
        },
        "coverage": {
            "identity": _coverage_entry(available=True),
            "learning": _coverage_entry(
                available=access.academics or access.assignments,
                window=window,
            ),
            "attendance": _coverage_entry(available=access.attendance, window=window),
            "family": _coverage_entry(available=access.family),
            "safeguarding": _coverage_entry(available=access.safeguarding),
            "finance": _coverage_entry(available=access.finance, window=window),
        },
        "warnings": [],
    }

    if access.academics or access.assignments:
        # The read model keeps one learning section. A caller with only one
        # component grant gets the authorized subset and an explicit warning.
        payload["learning"] = _learning(
            student=student,
            user=user,
            roles=roles,
            window=window,
            include_teachers=access.teachers,
        )
        if not access.academics:
            payload["learning"]["recent_grades"] = None
            payload["learning"]["recent_exam_results"] = None
            payload["learning"]["latest_transcript"] = None
        if not access.assignments:
            payload["learning"]["assignments"] = None
    if access.attendance:
        payload["attendance"] = _attendance(
            student=student,
            user=user,
            roles=roles,
            window=window,
        )
    if access.family:
        payload["family"] = _family(student=student, safeguarding=access.safeguarding)
        payload["warnings"].append(
            {
                "code": "family_verification_not_recorded",
                "message": "Contact verification and consent flags are not recorded by this service.",
                "affected_sections": ["family"],
            }
        )
    if access.finance:
        payload["finance"] = _finance(
            student=student,
            user=user,
            roles=roles,
            window=window,
        )
    if student.photo:
        payload["warnings"].append(
            {
                "code": "student_photo_unavailable",
                "message": "The stored photo cannot be served until its ownership is verified.",
                "affected_sections": ["identity.photo"],
            }
        )
    if payload["record_metadata"]["created_by"] is None:
        payload["warnings"].append(
            {
                "code": "record_actor_not_recorded",
                "message": "Creation and update actors were not recorded for this legacy record.",
                "affected_sections": ["record_metadata"],
            }
        )
    return payload
