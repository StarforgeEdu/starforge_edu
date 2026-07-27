"""Academics-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import factory

from apps.academics.models import Exam, ExamResult, ExamType, Grade, Subject
from apps.cohorts.tests.factories import CohortFactory
from apps.schedule.tests.factories import TermFactory
from apps.students.tests.factories import StudentProfileFactory


class SubjectFactory(factory.django.DjangoModelFactory[Subject]):
    class Meta:
        model = Subject

    name = factory.Sequence(lambda n: f"Subject {n}")
    code = factory.Sequence(lambda n: f"subj-{n}")


class ExamTypeFactory(factory.django.DjangoModelFactory[ExamType]):
    class Meta:
        model = ExamType

    name = factory.Sequence(lambda n: f"Exam Type {n}")
    slug = factory.Sequence(lambda n: f"exam-type-{n}")


class ExamFactory(factory.django.DjangoModelFactory[Exam]):
    class Meta:
        model = Exam

    subject = factory.SubFactory(SubjectFactory)
    cohort = factory.SubFactory(CohortFactory)
    term = factory.SubFactory(TermFactory)
    exam_type = factory.SubFactory(ExamTypeFactory)
    title = factory.Sequence(lambda n: f"Exam {n}")
    exam_date = date(2026, 3, 1)
    max_score = Decimal("100")
    weight = Decimal("1")


class ExamResultFactory(factory.django.DjangoModelFactory[ExamResult]):
    class Meta:
        model = ExamResult

    exam = factory.SubFactory(ExamFactory)
    student = factory.SubFactory(StudentProfileFactory)
    score = Decimal("80")


class GradeFactory(factory.django.DjangoModelFactory[Grade]):
    class Meta:
        model = Grade

    student = factory.SubFactory(StudentProfileFactory)
    subject = factory.SubFactory(SubjectFactory)
    term = factory.SubFactory(TermFactory)
    value_raw = Decimal("85")
    value_display = "85.0"
    is_published = False
