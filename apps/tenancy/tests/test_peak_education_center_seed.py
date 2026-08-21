from __future__ import annotations

from datetime import date

import pytest
from django_tenants.utils import schema_context

from apps.tenancy.management.commands.seed_peak_education_center import (
    Command,
    SeedConfig,
    _plan,
    _plan_digest,
)


def test_peak_seed_plan_is_deterministic_and_names_every_large_volume() -> None:
    config = SeedConfig(
        schema="starforge",
        seed_id="sfpeak-20260810-v1",
        random_seed=20260810,
        as_of="2026-08-10",
        students=1_200,
        teachers=60,
        history_days=370,
    )

    assert _plan(config) == _plan(config)
    assert _plan_digest(config) == _plan_digest(config)
    assert _plan(config)["students"] == 1_200
    assert _plan(config)["teachers"] == 60
    assert _plan(config)["attendance_records"] == 190_800
    assert _plan(config)["messages"] == 19_200


@pytest.mark.django_db
def test_peak_seed_builds_a_retry_safe_small_graph_across_guarded_models(tenant_a) -> None:
    from apps.academics.models import Exam, ExamLifecycleEvent, ExamResult, Grade
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher
    from apps.finance.models import Invoice, PaymentAllocation
    from apps.messaging.models import Message, Thread, ThreadParticipant
    from apps.org.models import Branch
    from apps.org.services import create_staff_account
    from apps.payments.models import Payment
    from apps.students.models import StudentProfile
    from apps.teachers.models import TeacherProfile
    from core.permissions import Role

    config = SeedConfig(
        schema=tenant_a.schema_name,
        seed_id="peak-integration-v1",
        random_seed=1729,
        as_of=date(2026, 8, 10).isoformat(),
        students=12,
        teachers=3,
        history_days=21,
    )
    student_prefix = f"sim.{config.username_token}.student."
    teacher_prefix = f"sim.{config.username_token}.teacher."

    with schema_context(tenant_a.schema_name):
        bootstrap_branch = Branch.objects.create(
            name="Seed bootstrap",
            slug="seed-bootstrap",
            address="Test",
            timezone="Asia/Tashkent",
            is_active=True,
            max_students=50,
            max_teachers=10,
        )
        create_staff_account(
            branch=bootstrap_branch,
            role=Role.DIRECTOR,
            username="seed-director",
            email="seed-director@example.invalid",
            first_name="Seed",
            last_name="Director",
        )

        command = Command()
        command._execute(config)
        first_counts = {
            "students": StudentProfile.objects.filter(username__startswith=student_prefix).count(),
            "teachers": TeacherProfile.objects.filter(username__startswith=teacher_prefix).count(),
            "cohorts": Cohort.objects.filter(name__startswith=config.marker).count(),
            "memberships": CohortMembership.objects.filter(
                student__username__startswith=student_prefix
            ).count(),
            "teacher_assignments": CohortTeacher.objects.filter(
                teacher__username__startswith=teacher_prefix
            ).count(),
            "attendance": AttendanceRecord.objects.filter(note__startswith=config.marker).count(),
            "invoices": Invoice.objects.filter(student__username__startswith=student_prefix).count(),
            "payments": Payment.objects.filter(
                idempotency_key__startswith="sim:peak-integration-v1:"
            ).count(),
            "allocations": PaymentAllocation.objects.filter(
                payment_id__in=Payment.objects.filter(
                    idempotency_key__startswith="sim:peak-integration-v1:"
                ).values("pk")
            ).count(),
            "exams": Exam.objects.filter(title__startswith=config.marker).count(),
            "results": ExamResult.objects.filter(exam__title__startswith=config.marker).count(),
            "grades": Grade.objects.filter(student__username__startswith=student_prefix).count(),
            "events": ExamLifecycleEvent.objects.filter(exam__title__startswith=config.marker).count(),
            "threads": Thread.objects.filter(subject__startswith=config.marker).count(),
            "participants": ThreadParticipant.objects.filter(
                thread__subject__startswith=config.marker
            ).count(),
            "messages": Message.objects.filter(body__startswith=config.marker).count(),
        }

        assert first_counts["students"] == 12
        assert first_counts["teachers"] == 3
        assert first_counts["cohorts"] == 3
        assert first_counts["memberships"] == 12
        assert first_counts["teacher_assignments"] == 3
        assert first_counts["attendance"] > 0
        assert first_counts["invoices"] == 144
        assert first_counts["payments"] == first_counts["allocations"]
        assert first_counts["exams"] == 12
        assert first_counts["results"] == 48
        assert first_counts["grades"] == 12
        assert first_counts["events"] == 12
        assert first_counts["threads"] == 12
        assert first_counts["participants"] == 36
        assert first_counts["messages"] == 192

        command._execute(config)
        assert StudentProfile.objects.filter(username__startswith=student_prefix).count() == 12
        assert Invoice.objects.filter(student__username__startswith=student_prefix).count() == 144
        assert Thread.objects.filter(subject__startswith=config.marker).count() == 12
        assert Message.objects.filter(body__startswith=config.marker).count() == 192
