"""Paired regression for committed tenant-data cleanup between tests.

The file is kept on one xdist worker by ``--dist=loadfile``. Test 01 commits a
sentinel in tenant A; schema-aware TransactionTestCase teardown must remove it
and restore the post-migration baseline before test 02 starts.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db(transaction=True)


SENTINEL_BRANCH = "transaction-cleanup-sentinel"


def test_01_transactional_tenant_row_is_committed(tenant_a, client_for):
    from apps.org.models import Branch

    with schema_context(tenant_a.schema_name):
        Branch.objects.create(name=SENTINEL_BRANCH)

    # Exercise the middleware behavior that originally made teardown depend on
    # whichever request happened to run last.
    response = client_for(tenant_a).get("/api/v1/users/me/")

    assert response.status_code == 401
    assert connection.schema_name == tenant_a.schema_name
    with schema_context(tenant_a.schema_name):
        assert Branch.objects.filter(name=SENTINEL_BRANCH).exists()


def test_02_committed_row_is_removed_and_seed_catalogues_survive(tenant_a):
    from apps.access.models import AccountType
    from apps.ai.models import AIPrompt
    from apps.crm.models import LeadSource, PipelineStage
    from apps.notifications.models import NotificationTemplate
    from apps.org.models import Branch, CenterSettings
    from apps.students.models import EnrollmentReason
    from apps.teachers.models import TeacherType

    with schema_context(tenant_a.schema_name):
        assert not Branch.objects.filter(name=SENTINEL_BRANCH).exists()
        assert CenterSettings.objects.filter(pk=1).exists()
        assert AccountType.objects.filter(slug="director", is_system=True, is_active=True).exists()
        assert set(TeacherType.objects.filter(is_system=True).values_list("slug", flat=True)) == {
            "main-teacher",
            "video-teacher",
            "assistant",
            "co-teacher",
        }
        assert PipelineStage.objects.filter(slug="new", is_active=True).exists()
        assert LeadSource.objects.filter(slug="other", is_active=True).exists()
        assert {
            "completed",
            "moved_city",
            "financial",
            "behavior",
            "schedule_conflict",
            "other",
        } <= set(EnrollmentReason.objects.values_list("slug", flat=True))
        assert NotificationTemplate.objects.filter(channel="in_app", is_active=True).exists()
        assert AIPrompt.objects.filter(is_active=True).exists()
