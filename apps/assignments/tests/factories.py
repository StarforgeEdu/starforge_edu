"""Assignments-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

from datetime import timedelta

import factory
from django.utils import timezone

from apps.assignments.models import Assignment, Submission
from apps.cohorts.tests.factories import CohortFactory
from apps.students.tests.factories import StudentProfileFactory


class AssignmentFactory(factory.django.DjangoModelFactory[Assignment]):
    class Meta:
        model = Assignment

    cohort = factory.SubFactory(CohortFactory)
    title = factory.Sequence(lambda n: f"Assignment {n}")
    due_at = factory.LazyFunction(lambda: timezone.now() + timedelta(days=7))
    status = Assignment.Status.PUBLISHED  # most tests want a submittable assignment


class SubmissionFactory(factory.django.DjangoModelFactory[Submission]):
    class Meta:
        model = Submission

    assignment = factory.SubFactory(AssignmentFactory)
    student = factory.SubFactory(StudentProfileFactory)
    attempt_number = 1
