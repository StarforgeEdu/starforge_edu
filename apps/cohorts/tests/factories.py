"""Cohort-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

from datetime import date

import factory

from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher
from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.teachers.tests.factories import TeacherProfileFactory, TeacherTypeFactory


class CohortFactory(factory.django.DjangoModelFactory[Cohort]):
    class Meta:
        model = Cohort
        django_get_or_create = ("branch", "name")

    name = factory.Sequence(lambda n: f"Cohort {n}")
    branch = factory.SubFactory(BranchFactory)
    start_date = date(2026, 1, 1)
    end_date = date(2026, 12, 31)


class CohortMembershipFactory(factory.django.DjangoModelFactory[CohortMembership]):
    """An ACTIVE membership by default (end_date is null)."""

    class Meta:
        model = CohortMembership

    cohort = factory.SubFactory(CohortFactory)
    student = factory.SubFactory(StudentProfileFactory)
    start_date = date(2026, 1, 1)
    end_date = None


class CohortTeacherFactory(factory.django.DjangoModelFactory[CohortTeacher]):
    class Meta:
        model = CohortTeacher

    cohort = factory.SubFactory(CohortFactory)
    teacher = factory.LazyAttribute(lambda assignment: TeacherProfileFactory(branch=assignment.cohort.branch))
    teacher_type = factory.SubFactory(TeacherTypeFactory)
