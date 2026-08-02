from __future__ import annotations

from datetime import date

from django.db import connection
from django_tenants.utils import schema_context

from apps.crm.models import LeadSource, PipelineStage
from apps.students.models import StudentProfile
from apps.students.tests.factories import StudentProfileFactory
from apps.users.tests.factories import UserFactory


def lead_student(*, branch, first_name="Aziza", last_name="Karimova", birthdate=None):
    user = UserFactory(
        first_name=first_name,
        last_name=last_name,
        birthdate=birthdate or date(2012, 5, 12),
    )
    student = StudentProfileFactory(
        user=user,
        branch=branch,
        status=StudentProfile.Status.LEAD,
    )
    # Preserve the fixture's tenant for helper catalogue lookups after the
    # surrounding schema_context exits. This attribute is test-only.
    student._crm_test_schema = connection.schema_name
    return student


def stage(slug: str = "new") -> PipelineStage:
    return PipelineStage.objects.get(slug=slug)


def create_lead(client, student, *, stage_slug="new", key=None, **extra):
    with schema_context(student._crm_test_schema):
        stage_id = stage(stage_slug).pk
        source_id = LeadSource.objects.get(slug="other").pk
    payload = {"student": student.pk, "stage": stage_id, "source": source_id, **extra}
    return client.post(
        "/api/v1/crm/leads/",
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or f"lead-create-{student.pk}",
    )
