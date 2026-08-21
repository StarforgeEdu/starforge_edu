"""Seed a realistic, repeatable local dataset for the CEO console.

Run ``scripts/seed_dev.py`` first so the ``demo`` tenant exists. This script is
development-only, never deletes rows, never imports test factories, and reserves
all of its mutable records with explicit ``[CEO demo]`` names or ``ceo-demo-*``
keys so a rerun can refresh only data it owns.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import django
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

from django_tenants.utils import get_public_schema_name, schema_context  # noqa: E402

from apps.academics import services as academics_services  # noqa: E402
from apps.academics.models import Exam, ExamResult, ExamType, Grade, Subject  # noqa: E402
from apps.approvals.models import ApprovalRequest  # noqa: E402
from apps.approvals.services import create_request  # noqa: E402
from apps.attendance.models import AttendanceRecord  # noqa: E402
from apps.cohorts.models import Cohort, CohortMembership  # noqa: E402
from apps.cohorts.services import (  # noqa: E402
    assign_cohort_teacher,
    enroll_student_in_cohort,
)
from apps.finance import services as finance_services  # noqa: E402
from apps.finance.models import (  # noqa: E402
    Expense,
    FeeSchedule,
    Invoice,
    PaymentAllocation,
    PaymentMethod,
    Refund,
)
from apps.finance.services import issue_invoice  # noqa: E402
from apps.meetings.models import MeetingAttendee, StaffMeeting  # noqa: E402
from apps.meetings.services import schedule_meeting  # noqa: E402
from apps.notifications.models import EventType, Notification  # noqa: E402
from apps.org.models import Branch, Department, StaffProfile  # noqa: E402
from apps.org.services import create_staff_account  # noqa: E402
from apps.payments import services as payment_services  # noqa: E402
from apps.payments.models import Payment  # noqa: E402
from apps.printing import services as printing_services  # noqa: E402
from apps.printing.models import BranchAgent, Printer, PrintJob  # noqa: E402
from apps.reports.models import Report, ReportKey  # noqa: E402
from apps.schedule.models import Lesson, Term  # noqa: E402
from apps.students.models import StudentProfile  # noqa: E402
from apps.students.services import block_student, create_student  # noqa: E402
from apps.tasks.models import Task  # noqa: E402
from apps.tasks.services import create_task, transition_task  # noqa: E402
from apps.teachers.models import TeacherProfile, TeacherType  # noqa: E402
from apps.teachers.services import create_teacher  # noqa: E402
from apps.tenancy.models import Center  # noqa: E402
from apps.users.models import User  # noqa: E402
from apps.users.services import (  # noqa: E402
    ensure_role_membership,
    set_role_account_password,
)
from core.historical_scope import (  # noqa: E402
    ATTRIBUTED_SCOPE_STATUSES,
    ScopeAttributionStatus,
)
from core.permissions import Role  # noqa: E402
from scripts.local_seed_safety import assert_local_seed_environment  # noqa: E402

DEMO_SCHEMA = "demo"
CEO_USERNAME = "admin"
LEGACY_CEO_USERNAME = "demo.director"
CEO_PHONE = "+998900000101"
# Deliberately weak and memorable for the throwaway local demonstration only.
# _assert_local_only refuses to run this seed outside explicit local development.
DEFAULT_CEO_PASSWORD = "root"
SEED_PREFIX = "[CEO demo]"


def _month_start(today: date, months_ago: int) -> date:
    absolute_month = today.year * 12 + today.month - 1 - months_ago
    return date(absolute_month // 12, absolute_month % 12 + 1, 1)


def _local_datetime(on: date, *, hour: int = 10) -> datetime:
    return timezone.make_aware(
        datetime.combine(on, time(hour=hour)),
        timezone.get_current_timezone(),
    )


TEACHERS = (
    {
        "username": "demo.teacher.dilshod",
        "phone": "+998901100101",
        "first_name": "Dilshod",
        "last_name": "Rahimov",
        "qualifications": "CELTA · Academic English",
        "subjects": ["English", "Academic writing"],
    },
    {
        "username": "demo.teacher.nargiza",
        "phone": "+998901100102",
        "first_name": "Nargiza",
        "last_name": "Usmonova",
        "qualifications": "Mathematics education",
        "subjects": ["Mathematics", "Problem solving"],
    },
    {
        "username": "demo.teacher.kamola",
        "phone": "+998901100103",
        "first_name": "Kamola",
        "last_name": "Ergasheva",
        "qualifications": "IELTS · Language coaching",
        "subjects": ["English", "Speaking"],
    },
    {
        "username": "demo.teacher.jasur",
        "phone": "+998901100104",
        "first_name": "Jasur",
        "last_name": "Tursunov",
        "qualifications": "STEM curriculum design",
        "subjects": ["Mathematics", "Science"],
    },
)


STUDENTS = (
    {
        "username": "demo.student.aziza",
        "phone": "+998902200101",
        "first_name": "Aziza",
        "last_name": "Karimova",
        "birthdate": date(2009, 9, 15),
        "gender": StudentProfile.Gender.FEMALE,
    },
    {
        "username": "demo.student.timur",
        "phone": "+998902200102",
        "first_name": "Timur",
        "last_name": "Abdullayev",
        "birthdate": date(2008, 11, 3),
        "gender": StudentProfile.Gender.MALE,
    },
    {
        "username": "demo.student.laylo",
        "phone": "+998902200103",
        "first_name": "Laylo",
        "last_name": "Nematova",
        "birthdate": date(2009, 8, 18),
        "gender": StudentProfile.Gender.FEMALE,
    },
    {
        "username": "demo.student.bekzod",
        "phone": "+998902200104",
        "first_name": "Bekzod",
        "last_name": "Saidov",
        "birthdate": date(2008, 12, 21),
        "gender": StudentProfile.Gender.MALE,
    },
    {
        "username": "demo.student.sabina",
        "phone": "+998902200105",
        "first_name": "Sabina",
        "last_name": "Yuldasheva",
        "birthdate": date(2009, 10, 7),
        "gender": StudentProfile.Gender.FEMALE,
    },
    {
        "username": "demo.student.akmal",
        "phone": "+998902200106",
        "first_name": "Akmal",
        "last_name": "Rasulov",
        "birthdate": date(2008, 8, 27),
        "gender": StudentProfile.Gender.MALE,
    },
    {
        "username": "demo.student.malika",
        "phone": "+998902200201",
        "first_name": "Malika",
        "last_name": "Khasanova",
        "birthdate": date(2009, 9, 2),
        "gender": StudentProfile.Gender.FEMALE,
    },
    {
        "username": "demo.student.sardor",
        "phone": "+998902200202",
        "first_name": "Sardor",
        "last_name": "Aliyev",
        "birthdate": date(2008, 10, 14),
        "gender": StudentProfile.Gender.MALE,
    },
    {
        "username": "demo.student.madina",
        "phone": "+998902200203",
        "first_name": "Madina",
        "last_name": "Toshpulatova",
        "birthdate": date(2009, 12, 1),
        "gender": StudentProfile.Gender.FEMALE,
    },
    {
        "username": "demo.student.diyor",
        "phone": "+998902200204",
        "first_name": "Diyor",
        "last_name": "Kurbanov",
        "birthdate": date(2008, 9, 29),
        "gender": StudentProfile.Gender.MALE,
    },
    {
        "username": "demo.student.zarina",
        "phone": "+998902200205",
        "first_name": "Zarina",
        "last_name": "Ismoilova",
        "birthdate": date(2009, 11, 19),
        "gender": StudentProfile.Gender.FEMALE,
    },
    {
        "username": "demo.student.shahzod",
        "phone": "+998902200206",
        "first_name": "Shahzod",
        "last_name": "Mamatov",
        "birthdate": date(2008, 8, 12),
        "gender": StudentProfile.Gender.MALE,
    },
)


def _assert_local_only(schema_name: str, password: str) -> None:
    assert_local_seed_environment(settings, project_root=PROJECT_ROOT)
    if not schema_name or schema_name == get_public_schema_name():
        raise RuntimeError("Choose a non-public development tenant schema.")
    if password == DEFAULT_CEO_PASSWORD:
        return
    if len(password) < 10:
        raise RuntimeError("STARFORGE_DEMO_CEO_PASSWORD must contain at least 10 characters.")
    try:
        validate_password(password)
    except ValidationError as exc:
        raise RuntimeError("STARFORGE_DEMO_CEO_PASSWORD does not meet the password policy.") from exc


def _ensure_ceo(branch: Branch, password: str) -> StaffProfile:
    ceo = StaffProfile.objects.select_related("user").filter(username=CEO_USERNAME).first()
    if ceo is None:
        collision = User.objects.filter(username=CEO_USERNAME).first()
        if collision is not None:
            # Older seed_dev releases created a tenant Django superuser called
            # ``admin``. Keep the product Director role-native and move only that
            # DEBUG-only admin identity out of the way before claiming the name.
            linked_role = StaffProfile.objects.filter(user_id=collision.pk).exists()
            if not (collision.is_staff and collision.is_superuser) or linked_role:
                raise RuntimeError(
                    f"Username {CEO_USERNAME!r} belongs to another account; refusing to replace it."
                )
            django_admin_username = "admin.django"
            if User.objects.filter(username=django_admin_username).exclude(pk=collision.pk).exists():
                raise RuntimeError(
                    f"Cannot move the local Django administrator to {django_admin_username!r}; "
                    "that username is already in use."
                )
            collision.username = django_admin_username
            collision.save(update_fields=["username"])

    legacy = StaffProfile.objects.select_related("user").filter(username=LEGACY_CEO_USERNAME).first()
    if ceo is None and legacy is not None:
        if User.objects.filter(username=CEO_USERNAME).exclude(pk=legacy.user_id).exists():
            raise RuntimeError(
                f"Username {CEO_USERNAME!r} already belongs to another account; refusing to replace it."
            )
        legacy.username = CEO_USERNAME
        legacy.user.username = CEO_USERNAME
        legacy.user.save(update_fields=["username"])
        legacy.save(update_fields=["username"])
        ceo = legacy
    elif ceo is not None and legacy is not None and ceo.pk != legacy.pk:
        raise RuntimeError("Both current and legacy CEO demo accounts exist; refusing to choose one.")
    if ceo is None:
        if User.objects.filter(username=CEO_USERNAME).exists():
            raise RuntimeError(
                f"Username {CEO_USERNAME!r} belongs to a non-role account; refusing to replace it."
            )
        ceo = create_staff_account(
            branch=branch,
            role=Role.DIRECTOR,
            username=CEO_USERNAME,
            phone=CEO_PHONE,
            email="director@demo.localhost",
            first_name="Demo",
            last_name="Director",
        )
    if ceo.user.is_staff or ceo.user.is_superuser:
        raise RuntimeError("The CEO role account must not bridge to a Django administrator.")
    if not ceo.is_active or not ceo.user.is_active:
        raise RuntimeError("The existing local CEO account is inactive; refusing to reactivate it.")
    ensure_role_membership(ceo, branch=branch, role=Role.DIRECTOR)
    if not ceo.check_password(password) or ceo.must_change_password:
        set_role_account_password(ceo, password, must_change=False)
    return ceo


def _ensure_branch(*, slug: str, name: str, address: str) -> tuple[Branch, Department]:
    branch, _ = Branch.objects.update_or_create(
        slug=slug,
        defaults={
            "name": name,
            "address": address,
            "phone": "+998712000000",
            "timezone": "Asia/Tashkent",
            "is_active": True,
            "max_students": 240,
            "max_teachers": 30,
        },
    )
    department, _ = Department.objects.update_or_create(
        branch=branch,
        slug="academic-programs",
        defaults={
            "name": "Academic Programs",
            "description": "Languages, mathematics, and student progress.",
            "is_active": True,
        },
    )
    return branch, department


def _ensure_teacher(spec: dict, branch: Branch, department: Department) -> TeacherProfile:
    teacher = TeacherProfile.objects.select_related("user").filter(username=spec["username"]).first()
    if teacher is not None:
        if teacher.branch_id != branch.pk or teacher.phone != spec["phone"]:
            raise RuntimeError(f"Seed identity collision for teacher {spec['username']!r}.")
        return teacher
    if User.objects.filter(username=spec["username"]).exists():
        raise RuntimeError(f"Seed username {spec['username']!r} is already used by another account.")
    return create_teacher(
        branch=branch,
        department=department,
        username=spec["username"],
        phone=spec["phone"],
        first_name=spec["first_name"],
        last_name=spec["last_name"],
        qualifications=spec["qualifications"],
        subjects=spec["subjects"],
        hire_date=date(2024, 8, 15),
        salary_type=TeacherProfile.SalaryType.MONTHLY,
        rate=Decimal("8500000.00"),
    )


def _ensure_student(spec: dict, branch: Branch, *, status: str) -> StudentProfile:
    student = StudentProfile.objects.select_related("user").filter(username=spec["username"]).first()
    if student is not None:
        if student.branch_id != branch.pk or student.phone != spec["phone"]:
            raise RuntimeError(f"Seed identity collision for student {spec['username']!r}.")
        return student
    if User.objects.filter(username=spec["username"]).exists():
        raise RuntimeError(f"Seed username {spec['username']!r} is already used by another account.")
    return create_student(
        branch=branch,
        username=spec["username"],
        phone=spec["phone"],
        first_name=spec["first_name"],
        last_name=spec["last_name"],
        birthdate=spec["birthdate"],
        gender=spec["gender"],
        status=status,
        academic_level="Intermediate",
        location=branch.name,
        previous_school="Local secondary school",
        skip_limit_check=True,
    )


def _ensure_cohort(
    *,
    branch: Branch,
    department: Department,
    name: str,
    teachers: list[TeacherProfile],
    students: list[StudentProfile],
    today: date,
) -> Cohort:
    cohort, _ = Cohort.objects.update_or_create(
        branch=branch,
        name=name,
        defaults={
            "department": department,
            "level": "Intermediate",
            "start_date": today - timedelta(days=90),
            "end_date": today + timedelta(days=180),
            "capacity": 18,
            "primary_teacher": teachers[0],
            "is_archived": False,
        },
    )
    teacher_types = {
        row.slug: row
        for row in TeacherType.objects.filter(slug__in=("main-teacher", "co-teacher"), is_active=True)
    }
    if set(teacher_types) != {"main-teacher", "co-teacher"}:
        raise RuntimeError("The seeded main-teacher and co-teacher types are required; run migrations.")
    assign_cohort_teacher(
        cohort=cohort,
        teacher=teachers[0],
        teacher_type=teacher_types["main-teacher"],
    )
    assign_cohort_teacher(
        cohort=cohort,
        teacher=teachers[1],
        teacher_type=teacher_types["co-teacher"],
    )
    for student in students:
        active = CohortMembership.objects.filter(
            cohort=cohort,
            student=student,
            end_date__isnull=True,
        ).exists()
        if not active:
            enroll_student_in_cohort(cohort=cohort, student=student)
    return cohort


def _ensure_term(today: date) -> Term:
    academic_year = f"{today.year}-{today.year + 1}"
    term = Term.objects.filter(name=f"{SEED_PREFIX} Operating term").first()
    values = {
        "academic_year": academic_year,
        "start_date": today - timedelta(days=210),
        "end_date": today + timedelta(days=180),
        "is_current": False,
    }
    if term is None:
        return Term.objects.create(name=f"{SEED_PREFIX} Operating term", **values)
    for field, value in values.items():
        setattr(term, field, value)
    term.save(update_fields=[*values, "updated_at"])
    return term


def _ensure_learning_signals(
    *,
    cohort: Cohort,
    department: Department,
    term: Term,
    students: list[StudentProfile],
    scores: tuple[int, ...],
    actor: User,
    today: date,
) -> Subject:
    code = f"ceo-demo-{cohort.branch.slug}"[:50]
    subject, _ = Subject.objects.update_or_create(
        code=code,
        defaults={
            "name": f"{cohort.branch.name} Core Studies",
            "department": department,
            "description": "Representative progress data for the local CEO console.",
            "is_active": True,
        },
    )
    exam_types = {
        row.slug: row
        for row in ExamType.objects.filter(slug__in=("quiz", "midterm", "final"), is_active=True)
    }
    if set(exam_types) != {"quiz", "midterm", "final"}:
        raise RuntimeError("The seeded quiz, midterm, and final exam types are required; run migrations.")

    exam_specs = (
        ("Foundation review", "quiz", 75, Decimal("0.750"), -6),
        ("Midpoint review", "midterm", 38, Decimal("1.250"), 3),
        ("Progress review", "final", 7, Decimal("1.750"), 0),
    )
    note = "Representative local progress result."
    for title, exam_type_slug, days_ago, weight, score_delta in exam_specs:
        exam_title = f"{SEED_PREFIX} {title}"
        exam = Exam.objects.filter(cohort=cohort, title=exam_title).first()
        exam_values = {
            "subject": subject,
            "term": term,
            "exam_type": exam_types[exam_type_slug],
            "exam_date": today - timedelta(days=days_ago),
            "max_score": Decimal("100"),
            "weight": weight,
            "created_by": actor,
        }
        if exam is None:
            exam = Exam.objects.create(cohort=cohort, title=exam_title, **exam_values)
        else:
            for field, value in exam_values.items():
                setattr(exam, field, value)
            exam.save(update_fields=[*exam_values, "updated_at"])

        rows = [
            {
                "student": student,
                "score": Decimal(max(0, min(100, score + score_delta))),
                "note": note,
            }
            for student, score in zip(students, scores, strict=True)
        ]
        existing = {
            result.student_id: result for result in ExamResult.objects.filter(exam=exam, student__in=students)
        }
        needs_write = len(existing) != len(rows) or any(
            row["student"].pk not in existing
            or existing[row["student"].pk].score != row["score"]
            or existing[row["student"].pk].note != note
            for row in rows
        )
        if needs_write:
            academics_services.record_results(exam=exam, rows=rows, actor=actor)
        academics_services.publish_exam(
            exam=exam,
            actor=actor,
            expected_version=exam.version,
            confirmed=True,
        )

    academics_services.recompute_cohort_term(
        cohort=cohort,
        subject=subject,
        term=term,
        publish=True,
    )
    return subject


def _ensure_attendance_signals(
    *,
    cohort: Cohort,
    teachers: list[TeacherProfile],
    students: list[StudentProfile],
    term: Term,
    now,
    at_risk_student: StudentProfile,
) -> None:
    local_anchor = timezone.localtime(now).replace(hour=10, minute=0, second=0, microsecond=0)
    lessons: list[Lesson] = []
    # Keep enough real observations to exercise monthly and multi-month
    # attendance views. Weekly sessions provide an honest time series without
    # manufacturing rows in the browser, and stable titles keep this seed
    # idempotent across repeated local runs.
    # Six students x sixteen sessions stays inside the current 100-row demo
    # attendance page while still spanning almost four calendar months.
    for index, days_ago in enumerate(range(4, 116, 7), start=1):
        starts_at = local_anchor - timedelta(days=days_ago)
        lesson, _ = Lesson.objects.update_or_create(
            cohort=cohort,
            title=f"{SEED_PREFIX} {cohort.branch.slug} session {index}",
            defaults={
                "term": term,
                "teacher": teachers[(index - 1) % len(teachers)],
                "starts_at": starts_at,
                "ends_at": starts_at + timedelta(minutes=80),
                "status": Lesson.Status.COMPLETED,
                "detached_from_rule": False,
                "cancel_reason": "",
            },
        )
        lessons.append(lesson)

    future_start = local_anchor + timedelta(days=2)
    Lesson.objects.update_or_create(
        cohort=cohort,
        title=f"{SEED_PREFIX} {cohort.branch.slug} next session",
        defaults={
            "term": term,
            "teacher": teachers[0],
            "starts_at": future_start,
            "ends_at": future_start + timedelta(minutes=80),
            "status": Lesson.Status.SCHEDULED,
            "detached_from_rule": False,
            "cancel_reason": "",
        },
    )

    for student_index, student in enumerate(students):
        for lesson_index, lesson in enumerate(lessons):
            if student.pk == at_risk_student.pk and lesson_index % 3 == 0:
                status = AttendanceRecord.Status.ABSENT
            elif student.pk == at_risk_student.pk and lesson_index % 5 == 0:
                status = AttendanceRecord.Status.LATE
            elif (student_index * 3 + lesson_index) % 19 == 0:
                status = AttendanceRecord.Status.EXCUSED
            elif (student_index + lesson_index) % 8 == 0:
                status = AttendanceRecord.Status.LATE
            else:
                status = AttendanceRecord.Status.PRESENT
            AttendanceRecord.objects.update_or_create(
                student=student,
                lesson=lesson,
                defaults={
                    "status": status,
                    "note": "Representative local attendance record.",
                    "marked_by": lesson.teacher.user,
                    "auto_marked": False,
                },
            )


def _ensure_finance(
    *,
    cohort: Cohort,
    students: list[StudentProfile],
    actor: User,
    today: date,
) -> list[Invoice]:
    schedule, _ = FeeSchedule.objects.update_or_create(
        cohort=cohort,
        name=f"{SEED_PREFIX} Monthly tuition",
        defaults={
            "amount_uzs": Decimal("1250000.00"),
            "billing_period": FeeSchedule.BillingPeriod.MONTHLY,
            "due_day_of_month": 5,
            "is_active": True,
        },
    )
    invoices: list[Invoice] = []
    invoice_specs = (
        (students[0], "ceo-demo-1", 3, False),
        (students[1], "ceo-demo-2", 2, False),
        (students[2], "ceo-demo-3", 1, True),
        (students[3], "ceo-demo-4", 0, False),
    )
    for student, period, months_ago, should_be_overdue in invoice_specs:
        invoice = Invoice.objects.filter(
            student=student,
            fee_schedule=schedule,
            period=period,
        ).first()
        if invoice is None:
            invoice = issue_invoice(
                student_id=student.pk,
                fee_schedule_id=schedule.pk,
                period=period,
                created_by=actor,
                apply_discounts=False,
            )
        elif invoice.attribution_status not in ATTRIBUTED_SCOPE_STATUSES:
            # Historical-scope migrations deliberately quarantine legacy rows
            # instead of guessing ownership. These rows are safe to resolve: the
            # local-only seed owns the fee schedule, student and cohort, and the
            # three records agree on the same branch.
            if (
                student.branch_id != cohort.branch_id
                or invoice.student_id != student.pk
                or invoice.fee_schedule_id != schedule.pk
            ):
                raise RuntimeError(f"Cannot resolve CEO demo invoice scope for {invoice.number!r}.")
            Invoice.objects.filter(pk=invoice.pk).update(
                branch_at_issue_id=cohort.branch_id,
                department_at_issue_id=cohort.department_id,
                attribution_status=ScopeAttributionStatus.RESOLVED,
            )
            invoice.refresh_from_db()
        issue_date = _month_start(today, months_ago)
        due_date = issue_date + timedelta(days=9)
        allocated = sum(invoice.allocations.values_list("amount_uzs", flat=True), Decimal("0"))
        if allocated >= invoice.total_uzs and invoice.total_uzs > 0:
            status = Invoice.Status.PAID
        elif allocated > 0:
            status = Invoice.Status.PARTIALLY_PAID
        elif should_be_overdue:
            status = Invoice.Status.OVERDUE
        else:
            status = Invoice.Status.ISSUED
        invoice.issue_date = issue_date
        invoice.due_date = due_date
        invoice.status = status
        invoice.save(update_fields=["issue_date", "due_date", "status", "updated_at"])
        Invoice.objects.filter(pk=invoice.pk).update(created_at=_local_datetime(issue_date))
        invoices.append(invoice)
    return invoices


def _validate_payment_identity(
    payment: Payment,
    *,
    key: str,
    provider: str,
    amount: Decimal,
    invoice: Invoice,
) -> None:
    if (
        payment.idempotency_key != key
        or payment.provider != provider
        or payment.amount_uzs != amount
        or payment.account_ref != invoice.number
        or str(payment.metadata.get("invoice_id")) != str(invoice.pk)
    ):
        raise RuntimeError(f"Seed payment identity collision for {key!r}.")


def _repair_seed_payment_scope(*, key: str, invoice: Invoice) -> None:
    """Resolve an owned pre-snapshot demo payment from its immutable invoice."""

    payment = Payment.objects.filter(idempotency_key=key).first()
    if payment is None or payment.attribution_status in ATTRIBUTED_SCOPE_STATUSES:
        return
    if (
        payment.account_ref != invoice.number
        or str(payment.metadata.get("invoice_id")) != str(invoice.pk)
        or invoice.attribution_status not in ATTRIBUTED_SCOPE_STATUSES
        or invoice.branch_at_issue_id is None
    ):
        raise RuntimeError(f"Cannot resolve CEO demo payment scope for {key!r}.")
    Payment.objects.filter(pk=payment.pk).update(
        branch_at_payment_id=invoice.branch_at_issue_id,
        department_at_payment_id=invoice.department_at_issue_id,
        attribution_status=ScopeAttributionStatus.RESOLVED,
    )


def _ensure_completed_payment(
    *,
    key: str,
    invoice: Invoice,
    amount: Decimal,
    paid_on: date,
    provider: str,
) -> Payment:
    _repair_seed_payment_scope(key=key, invoice=invoice)
    payment, _ = payment_services.get_or_create_payment(
        idempotency_key=key,
        provider=provider,
        amount_uzs=amount,
        account_ref=invoice.number,
        payer=invoice.student.user,
        metadata={"invoice_id": invoice.pk, "student_id": invoice.student_id, "demo": True},
        invoice=invoice,
    )
    _validate_payment_identity(
        payment,
        key=key,
        provider=provider,
        amount=amount,
        invoice=invoice,
    )
    if payment.status in (Payment.Status.PENDING, Payment.Status.PROCESSING):
        payment_services.mark_payment_completed(
            payment_id=payment.pk,
            provider_txn_id=f"demo:{key}",
            auto_allocate=False,
        )
    elif payment.status not in (Payment.Status.COMPLETED, Payment.Status.REFUNDED):
        raise RuntimeError(f"Seed payment {key!r} is unexpectedly {payment.status!r}.")
    if not PaymentAllocation.objects.filter(payment_id=payment.pk).exists():
        payment_services.allocate_manual(
            payment_id=payment.pk,
            allocations=[{"invoice": invoice.pk, "amount": amount}],
        )
    paid_at = _local_datetime(paid_on, hour=14)
    Payment.objects.filter(pk=payment.pk).update(created_at=paid_at, paid_at=paid_at)
    payment.refresh_from_db()
    return payment


def _ensure_unsettled_payment(
    *,
    key: str,
    invoice: Invoice,
    amount: Decimal,
    created_on: date,
    fail: bool,
) -> Payment:
    _repair_seed_payment_scope(key=key, invoice=invoice)
    payment, _ = payment_services.get_or_create_payment(
        idempotency_key=key,
        provider=Payment.Method.CLICK,
        amount_uzs=amount,
        account_ref=invoice.number,
        payer=invoice.student.user,
        metadata={"invoice_id": invoice.pk, "student_id": invoice.student_id, "demo": True},
        invoice=invoice,
    )
    _validate_payment_identity(
        payment,
        key=key,
        provider=Payment.Method.CLICK,
        amount=amount,
        invoice=invoice,
    )
    if fail and payment.status in (Payment.Status.PENDING, Payment.Status.PROCESSING):
        payment = payment_services.mark_payment_failed(payment_id=payment.pk)
    expected = Payment.Status.FAILED if fail else Payment.Status.PENDING
    if payment.status != expected:
        raise RuntimeError(f"Seed payment {key!r} is unexpectedly {payment.status!r}.")
    Payment.objects.filter(pk=payment.pk).update(created_at=_local_datetime(created_on, hour=16))
    payment.refresh_from_db()
    return payment


def _ensure_refund(
    *,
    invoice: Invoice,
    payment: Payment,
    requester: User,
    approver: User,
    refunded_on: date,
) -> Refund:
    reason = f"{SEED_PREFIX} Family schedule adjustment"
    provider_reference = "ceo-demo-refund-riverside-01"
    matches = list(
        Refund.objects.filter(
            invoice=invoice,
            payment_id=payment.pk,
            reason=reason,
        )
    )
    if len(matches) > 1:
        raise RuntimeError("Duplicate CEO demo refunds detected.")
    refund = (
        matches[0]
        if matches
        else finance_services.request_refund(
            invoice=invoice,
            amount_uzs=Decimal("175000.00"),
            reason=reason,
            payment_id=payment.pk,
            requested_by=requester,
            provider=Payment.Method.BANK_TRANSFER,
        )
    )
    if refund.amount_uzs != Decimal("175000.00"):
        raise RuntimeError("The CEO demo refund identity has an unexpected amount.")
    if refund.state == Refund.State.REQUESTED:
        refund = finance_services.transition_refund(
            refund_id=refund.pk,
            to_state=Refund.State.APPROVED,
            actor=approver,
        )
    if refund.state == Refund.State.APPROVED:
        refund = finance_services.transition_refund(
            refund_id=refund.pk,
            to_state=Refund.State.SENT_TO_PROVIDER,
            actor=approver,
        )
    if refund.state in (Refund.State.SENT_TO_PROVIDER, Refund.State.COMPLETED):
        refund = finance_services.register_refund_completion(
            refund.pk,
            payment.pk,
            provider=Payment.Method.BANK_TRANSFER,
            provider_refund_id=provider_reference,
        )
    else:
        raise RuntimeError(f"The CEO demo refund is unexpectedly {refund.state!r}.")
    refunded_at = _local_datetime(refunded_on, hour=15)
    Refund.objects.filter(pk=refund.pk).update(created_at=refunded_at, updated_at=refunded_at)
    refund.refresh_from_db()
    return refund


def _ensure_payments_and_refund(
    *,
    central_invoices: list[Invoice],
    riverside_invoices: list[Invoice],
    teachers: list[TeacherProfile],
    ceo: StaffProfile,
    today: date,
) -> tuple[list[Payment], Refund]:
    bank = Payment.Method.BANK_TRANSFER
    payment_specs = (
        ("ceo-demo-pay-central-full", central_invoices[0], Decimal("1250000.00"), 3, bank),
        ("ceo-demo-pay-central-part", central_invoices[1], Decimal("620000.00"), 2, Payment.Method.CASH),
        ("ceo-demo-pay-riverside-refund", riverside_invoices[0], Decimal("1250000.00"), 3, bank),
        ("ceo-demo-pay-riverside-full", riverside_invoices[1], Decimal("1250000.00"), 2, bank),
        ("ceo-demo-pay-riverside-part", riverside_invoices[2], Decimal("780000.00"), 1, Payment.Method.CASH),
    )
    payments = [
        _ensure_completed_payment(
            key=key,
            invoice=invoice,
            amount=amount,
            paid_on=_month_start(today, months_ago) + timedelta(days=5),
            provider=provider,
        )
        for key, invoice, amount, months_ago, provider in payment_specs
    ]
    payments.extend(
        [
            _ensure_unsettled_payment(
                key="ceo-demo-pay-central-failed",
                invoice=central_invoices[2],
                amount=central_invoices[2].total_uzs,
                created_on=_month_start(today, 1) + timedelta(days=4),
                fail=True,
            ),
            _ensure_unsettled_payment(
                key="ceo-demo-pay-riverside-pending",
                invoice=riverside_invoices[3],
                amount=riverside_invoices[3].total_uzs,
                created_on=_month_start(today, 0),
                fail=False,
            ),
        ]
    )
    refund = _ensure_refund(
        invoice=riverside_invoices[0],
        payment=payments[2],
        requester=teachers[2].user,
        approver=ceo.user,
        refunded_on=_month_start(today, 2) + timedelta(days=12),
    )
    return payments, refund


def _ensure_expenses(
    *,
    central: Branch,
    riverside: Branch,
    teachers: list[TeacherProfile],
    ceo: StaffProfile,
    today: date,
) -> list[Expense]:
    payment_method, _ = PaymentMethod.objects.update_or_create(
        slug="ceo-demo-bank-transfer",
        defaults={"name": "Bank transfer", "is_active": True},
    )
    specs = (
        (
            central,
            f"{SEED_PREFIX} Central classroom lease",
            Decimal("4800000.00"),
            "Facilities",
            Expense.Status.PAID,
            teachers[0].user,
            teachers[1].user,
            3,
        ),
        (
            riverside,
            f"{SEED_PREFIX} Riverside learning materials",
            Decimal("2350000.00"),
            "Learning resources",
            Expense.Status.PAID,
            teachers[2].user,
            teachers[3].user,
            2,
        ),
        (
            central,
            f"{SEED_PREFIX} Classroom display upgrade",
            Decimal("6200000.00"),
            "Technology",
            Expense.Status.APPROVED,
            teachers[0].user,
            teachers[1].user,
            0,
        ),
        (
            riverside,
            f"{SEED_PREFIX} August utilities",
            Decimal("1850000.00"),
            "Utilities",
            Expense.Status.PENDING,
            teachers[2].user,
            teachers[3].user,
            0,
        ),
    )
    expenses: list[Expense] = []
    for branch, description, amount, category, target_status, requester, disburser, months_ago in specs:
        matches = list(Expense.objects.filter(branch=branch, description=description))
        if len(matches) > 1:
            raise RuntimeError(f"Duplicate CEO demo expenses detected for {description!r}.")
        expense = (
            matches[0]
            if matches
            else finance_services.create_expense(
                branch=branch,
                description=description,
                amount_uzs=amount,
                category=category,
                created_by=requester,
            )
        )
        if expense.amount_uzs != amount or expense.category != category:
            raise RuntimeError(f"Seed expense identity collision for {description!r}.")
        if target_status in (Expense.Status.APPROVED, Expense.Status.PAID):
            if expense.status == Expense.Status.PENDING:
                expense = finance_services.approve_expense(expense_id=expense.pk, actor=ceo.user)
            if expense.status not in (Expense.Status.APPROVED, Expense.Status.PAID):
                raise RuntimeError(f"Seed expense {description!r} is unexpectedly {expense.status!r}.")
        if target_status == Expense.Status.PAID and expense.status == Expense.Status.APPROVED:
            expense = finance_services.pay_expense(
                expense_id=expense.pk,
                payment_method_id=payment_method.pk,
                actor=disburser,
            )
        if expense.status != target_status:
            raise RuntimeError(f"Seed expense {description!r} is unexpectedly {expense.status!r}.")

        created_on = _month_start(today, months_ago) + timedelta(days=2)
        created_at = _local_datetime(created_on, hour=11)
        update_fields = {"created_at": created_at}
        if expense.approved_at:
            update_fields["approved_at"] = created_at + timedelta(days=1)
        if expense.paid_at:
            update_fields["paid_at"] = created_at + timedelta(days=2)
        Expense.objects.filter(pk=expense.pk).update(**update_fields)
        expense.refresh_from_db()
        if expense.approval_request_id:
            approval_updates = {"created_at": created_at}
            if expense.approval_request.decided_at:
                approval_updates["decided_at"] = created_at + timedelta(days=1)
            if expense.approval_request.disbursed_at:
                approval_updates["disbursed_at"] = created_at + timedelta(days=2)
            ApprovalRequest.objects.filter(pk=expense.approval_request_id).update(**approval_updates)
        expenses.append(expense)
    return expenses


def _ensure_print_room(
    *,
    schema_name: str,
    branch: Branch,
    cohort: Cohort,
    students: list[StudentProfile],
    invoices: list[Invoice],
    actor: User,
    now: datetime,
) -> tuple[Printer, BranchAgent, list[PrintJob]]:
    """Create representative branch print operations without provisioning a device.

    The connection token is random, stored only as a hash by the domain service, and
    deliberately discarded. The seeded connection is an operational-history record;
    it cannot be used to authenticate a real print bridge. All mutable records carry
    a reserved CEO-demo name/key so reruns touch only records owned by this script.
    """
    printer_name = f"{SEED_PREFIX} Learning office printer"
    printer, _ = Printer.objects.update_or_create(
        branch=branch,
        name=printer_name,
        defaults={
            "model_name": "Office Color MFP" if branch.slug.endswith("central") else "Office MFP",
            "capabilities": {
                "color": branch.slug.endswith("central"),
                "duplex": True,
                "paper": ["A4", "A5", "LETTER"],
            },
            "is_active": True,
        },
    )

    agent_name = f"{SEED_PREFIX} Local print connection"
    agent_matches = list(BranchAgent.objects.filter(branch=branch, name=agent_name))
    if len(agent_matches) > 1:
        raise RuntimeError(f"Duplicate CEO demo print connections detected for {branch.slug!r}.")
    if agent_matches:
        agent = agent_matches[0]
        if agent.created_by_id not in (None, actor.pk):
            raise RuntimeError(f"Seed print connection identity collision for {branch.slug!r}.")
        if agent.revoked_at is not None:
            raise RuntimeError(
                f"The CEO demo print connection for {branch.slug!r} was revoked; "
                "refusing to silently restore its credential."
            )
    else:
        agent, raw_token = printing_services.register_agent(
            branch_id=branch.pk,
            name=agent_name,
            created_by=actor,
        )
        # Registration must use the production-safe token path, but no demo device
        # needs the credential. Never persist it outside BranchAgent's one-way hash.
        del raw_token
    BranchAgent.objects.filter(pk=agent.pk).update(
        created_by=actor,
        last_seen_at=now - timedelta(minutes=4),
    )
    agent.refresh_from_db()

    reports = {
        report.key: report
        for report in Report.objects.filter(key__in=(ReportKey.ATTENDANCE, ReportKey.FINANCE))
    }
    if set(reports) != {ReportKey.ATTENDANCE, ReportKey.FINANCE}:
        raise RuntimeError("The attendance and finance report library entries are required; run migrations.")
    if len(students) < 2 or len(invoices) < 1:
        raise RuntimeError(f"Print demo sources are incomplete for branch {branch.slug!r}.")

    specs = (
        {
            "key": "tuition-receipt-completed",
            "source": PrintJob.Source.RECEIPT,
            "source_id": invoices[0].pk,
            "status": PrintJob.Status.DONE,
            "pages": 2,
            "copies": 1,
            "color": False,
            "duplex": False,
            "age": timedelta(days=18),
        },
        {
            "key": "attendance-report-completed",
            "source": PrintJob.Source.REPORT,
            "source_id": reports[ReportKey.ATTENDANCE].pk,
            "status": PrintJob.Status.DONE,
            "pages": 8,
            "copies": 2,
            "color": branch.slug.endswith("central"),
            "duplex": True,
            "age": timedelta(days=9),
        },
        {
            "key": "student-transcript-queued",
            "source": PrintJob.Source.TRANSCRIPT,
            "source_id": students[1].pk,
            "status": PrintJob.Status.QUEUED,
            "pages": 3,
            "copies": 1,
            "color": False,
            "duplex": True,
            "age": timedelta(minutes=70),
        },
        {
            "key": "finance-report-failed",
            "source": PrintJob.Source.REPORT,
            "source_id": reports[ReportKey.FINANCE].pk,
            "status": PrintJob.Status.FAILED,
            "pages": 6,
            "copies": 3,
            "color": False,
            "duplex": True,
            "age": timedelta(days=3),
        },
    )

    jobs: list[PrintJob] = []
    for spec in specs:
        payload_key = f"{schema_name}/ceo-demo/print-room/{branch.slug}/{spec['key']}.pdf"
        matches = list(PrintJob.objects.filter(branch=branch, payload_s3_key=payload_key))
        if len(matches) > 1:
            raise RuntimeError(f"Duplicate CEO demo print jobs detected for {payload_key!r}.")
        if matches:
            job = matches[0]
        else:
            job = printing_services.enqueue_print(
                source=spec["source"],
                source_id=spec["source_id"],
                payload_s3_key=payload_key,
                branch_id=branch.pk,
                requested_by=actor,
                pages=spec["pages"],
                copies=spec["copies"],
                color=spec["color"],
                duplex=spec["duplex"],
                cohort_id=cohort.pk,
            )
        if job.source != spec["source"] or job.source_id != spec["source_id"] or job.cohort_id != cohort.pk:
            raise RuntimeError(f"Seed print job identity collision for {payload_key!r}.")

        created_at = now - spec["age"]
        terminal = spec["status"] in (PrintJob.Status.DONE, PrintJob.Status.FAILED)
        completed = spec["status"] == PrintJob.Status.DONE
        failed = spec["status"] == PrintJob.Status.FAILED
        PrintJob.objects.filter(pk=job.pk).update(
            requested_by=actor,
            printer=printer if terminal else None,
            agent=agent if completed else None,
            status=spec["status"],
            pages=spec["pages"],
            copies=spec["copies"],
            color=spec["color"],
            duplex=spec["duplex"],
            attempts=3 if failed else 0,
            next_attempt_at=now - timedelta(minutes=5) if spec["status"] == PrintJob.Status.QUEUED else None,
            pages_printed=spec["pages"] * spec["copies"] if completed else (1 if failed else 0),
            last_error="Paper supply needs attention." if failed else "",
            created_at=created_at,
            claimed_at=created_at + timedelta(minutes=2) if terminal else None,
            finished_at=created_at + timedelta(minutes=7) if terminal else None,
        )
        job.refresh_from_db()
        jobs.append(job)
    return printer, agent, jobs


def _ensure_work_queue(
    *,
    ceo: StaffProfile,
    branches: list[Branch],
    departments: list[Department],
    teachers: list[TeacherProfile],
    now,
) -> None:
    request_title = f"{SEED_PREFIX} Learning-space technology refresh"
    if not ApprovalRequest.objects.filter(kind="procurement", title=request_title).exists():
        create_request(
            kind="procurement",
            title=request_title,
            requested_by=teachers[0].user,
            amount_uzs=Decimal("4850000.00"),
            description="Equip a classroom with a display and collaborative learning tools.",
            branch=branches[0],
            payload={"purpose": "classroom technology", "priority": "high"},
        )

    task_title = f"{SEED_PREFIX} Confirm next-month cohort capacity"
    task = Task.objects.filter(title=task_title, created_by=ceo.user).first()
    if task is None:
        task = create_task(
            title=task_title,
            description="Review enrollment demand, teaching capacity, and available rooms.",
            created_by=ceo.user,
            created_by_roles={Role.DIRECTOR},
            assignee=teachers[1].user,
            department=departments[0],
            branch=branches[0],
            priority=Task.Priority.HIGH,
            due_at=now + timedelta(days=3),
        )
        transition_task(
            task=task,
            to_status=Task.Status.IN_PROGRESS,
            actor=ceo.user,
            can_transition_any=True,
        )

    meeting_title = f"{SEED_PREFIX} Academic leadership review"
    meeting = StaffMeeting.objects.filter(title=meeting_title, created_by=ceo.user).first()
    starts_at = timezone.localtime(now).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if meeting is None:
        meeting = schedule_meeting(
            title=meeting_title,
            agenda="Student support signals, branch performance, and next-month capacity.",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=60),
            location="Central Campus · Leadership room",
            attendees=[ceo.user, *(teacher.user for teacher in teachers)],
            created_by=ceo.user,
            branch=branches[0],
        )
    else:
        meeting.starts_at = starts_at
        meeting.ends_at = starts_at + timedelta(minutes=60)
        meeting.status = StaffMeeting.Status.SCHEDULED
        meeting.cancelled_by = None
        meeting.cancelled_at = None
        meeting.save(
            update_fields=[
                "starts_at",
                "ends_at",
                "status",
                "cancelled_by",
                "cancelled_at",
            ]
        )
        invitees = [(ceo.user, "staff", ceo.pk)] + [
            (teacher.user, "teacher", teacher.pk) for teacher in teachers
        ]
        for user, principal_kind, principal_id in invitees:
            MeetingAttendee.objects.update_or_create(
                meeting=meeting,
                user=user,
                defaults={
                    "principal_kind": principal_kind,
                    "principal_id": principal_id,
                },
            )

    Notification.objects.get_or_create(
        dedupe_key="ceo-console-demo:student-support",
        defaults={
            "user": ceo.user,
            "event_type": EventType.REPORT_READY,
            "title": "Student support review is ready",
            "body": "The latest attendance, progress, and payment signals are available.",
            "data": {"route": "insights/student-risk"},
        },
    )
    Notification.objects.get_or_create(
        dedupe_key="ceo-console-demo:leadership-meeting",
        defaults={
            "user": ceo.user,
            "event_type": EventType.SCHEDULE_LESSON_REMINDER,
            "title": "Academic leadership review scheduled",
            "body": "Tomorrow's operating review is on the leadership calendar.",
            "data": {"route": "schedule/upcoming", "meeting_id": meeting.pk},
        },
    )


def main() -> None:
    schema_name = os.getenv("STARFORGE_DEMO_SCHEMA", DEMO_SCHEMA).strip()
    password = os.getenv("STARFORGE_DEMO_CEO_PASSWORD", DEFAULT_CEO_PASSWORD)
    _assert_local_only(schema_name, password)

    center = Center.objects.filter(schema_name=schema_name, is_active=True).first()
    if center is None:
        raise RuntimeError(
            f"Development tenant {schema_name!r} does not exist. Run scripts/seed_dev.py first."
        )

    now = timezone.now()
    today = timezone.localdate()
    with schema_context(schema_name), transaction.atomic():
        central, central_department = _ensure_branch(
            slug="ceo-demo-central",
            name="Central Campus",
            address="12 Amir Temur Avenue, Tashkent",
        )
        riverside, riverside_department = _ensure_branch(
            slug="ceo-demo-riverside",
            name="Riverside Campus",
            address="45 Mirzo Ulugbek Street, Tashkent",
        )
        ceo = _ensure_ceo(central, password)

        teachers = [
            _ensure_teacher(
                spec,
                central if index < 2 else riverside,
                central_department if index < 2 else riverside_department,
            )
            for index, spec in enumerate(TEACHERS)
        ]
        central_students = [
            _ensure_student(spec, central, status=StudentProfile.Status.ACTIVE) for spec in STUDENTS[:6]
        ]
        riverside_students = [
            _ensure_student(spec, riverside, status=StudentProfile.Status.ACTIVE) for spec in STUDENTS[6:]
        ]
        waiting_student = _ensure_student(
            {
                "username": "demo.student.mohira",
                "phone": "+998902200301",
                "first_name": "Mohira",
                "last_name": "Olimova",
                "birthdate": date(2009, 12, 12),
                "gender": StudentProfile.Gender.FEMALE,
            },
            central,
            status=StudentProfile.Status.ACCEPTED,
        )

        central_cohort = _ensure_cohort(
            branch=central,
            department=central_department,
            name=f"{SEED_PREFIX} Nova B1",
            teachers=teachers[:2],
            students=central_students,
            today=today,
        )
        riverside_cohort = _ensure_cohort(
            branch=riverside,
            department=riverside_department,
            name=f"{SEED_PREFIX} Horizon B1",
            teachers=teachers[2:],
            students=riverside_students,
            today=today,
        )
        if not riverside_students[2].is_blocked:
            block_student(
                student=riverside_students[2],
                reason="Family follow-up and enrollment-document review.",
                actor=ceo.user,
            )

        term = _ensure_term(today)
        _ensure_learning_signals(
            cohort=central_cohort,
            department=central_department,
            term=term,
            students=central_students,
            scores=(42, 88, 79, 91, 73, 84),
            actor=ceo.user,
            today=today,
        )
        _ensure_learning_signals(
            cohort=riverside_cohort,
            department=riverside_department,
            term=term,
            students=riverside_students,
            scores=(86, 77, 48, 93, 81, 75),
            actor=ceo.user,
            today=today,
        )
        _ensure_attendance_signals(
            cohort=central_cohort,
            teachers=teachers[:2],
            students=central_students,
            term=term,
            now=now,
            at_risk_student=central_students[0],
        )
        _ensure_attendance_signals(
            cohort=riverside_cohort,
            teachers=teachers[2:],
            students=riverside_students,
            term=term,
            now=now,
            at_risk_student=riverside_students[2],
        )
        central_invoices = _ensure_finance(
            cohort=central_cohort,
            students=central_students,
            actor=ceo.user,
            today=today,
        )
        riverside_invoices = _ensure_finance(
            cohort=riverside_cohort,
            students=riverside_students,
            actor=ceo.user,
            today=today,
        )
        invoices = [*central_invoices, *riverside_invoices]
        payments, _refund = _ensure_payments_and_refund(
            central_invoices=central_invoices,
            riverside_invoices=riverside_invoices,
            teachers=teachers,
            ceo=ceo,
            today=today,
        )
        expenses = _ensure_expenses(
            central=central,
            riverside=riverside,
            teachers=teachers,
            ceo=ceo,
            today=today,
        )
        central_printer, central_agent, central_print_jobs = _ensure_print_room(
            schema_name=schema_name,
            branch=central,
            cohort=central_cohort,
            students=central_students,
            invoices=central_invoices,
            actor=ceo.user,
            now=now,
        )
        riverside_printer, riverside_agent, riverside_print_jobs = _ensure_print_room(
            schema_name=schema_name,
            branch=riverside,
            cohort=riverside_cohort,
            students=riverside_students,
            invoices=riverside_invoices,
            actor=ceo.user,
            now=now,
        )
        printers = [central_printer, riverside_printer]
        print_agents = [central_agent, riverside_agent]
        print_jobs = [*central_print_jobs, *riverside_print_jobs]
        _ensure_work_queue(
            ceo=ceo,
            branches=[central, riverside],
            departments=[central_department, riverside_department],
            teachers=teachers,
            now=now,
        )
        result_count = ExamResult.objects.filter(exam__title__startswith=SEED_PREFIX).count()
        grade_count = Grade.objects.filter(subject__code__startswith="ceo-demo-").count()
        invoice_count = Invoice.objects.filter(fee_schedule__name__startswith=SEED_PREFIX).count()
        seeded_lessons = Lesson.objects.filter(
            cohort__in=(central_cohort, riverside_cohort),
            title__startswith=SEED_PREFIX,
        )
        lesson_count = seeded_lessons.count()
        attendance_count = AttendanceRecord.objects.filter(lesson__in=seeded_lessons).count()

    print("CEO console demo seed complete")
    print(f"tenant: http://{schema_name}.localhost:8000")
    print(f"role login: {CEO_USERNAME}")
    if password == DEFAULT_CEO_PASSWORD:
        print(f"local default password: {DEFAULT_CEO_PASSWORD}")
    else:
        print("password: value supplied through STARFORGE_DEMO_CEO_PASSWORD")
    print(
        "seeded: 1 CEO, 2 campuses, 4 teachers, "
        f"{len(central_students) + len(riverside_students) + 1} students, "
        f"2 cohorts, {lesson_count} lessons, {attendance_count} attendance marks, "
        f"{result_count} results, {grade_count} grades, "
        f"{invoice_count} invoices ({len(invoices)} historical chart records), "
        f"{len(payments)} payments, 1 completed refund, "
        f"{len(expenses)} expenses, "
        f"{len(printers)} printers, {len(print_agents)} print connections, "
        f"{len(print_jobs)} print jobs "
        f"({sum(job.status == PrintJob.Status.DONE for job in print_jobs)} completed, "
        f"{sum(job.status == PrintJob.Status.QUEUED for job in print_jobs)} queued, "
        f"{sum(job.status == PrintJob.Status.FAILED for job in print_jobs)} failed)"
    )
    print(f"waiting for cohort placement: {waiting_student.get_full_name()}")


if __name__ == "__main__":
    main()
