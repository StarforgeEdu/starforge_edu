from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections
from django_tenants.utils import schema_context

from apps.crm.dto import CRMScope, LeadCreateDTO
from apps.crm.models import CRMIdempotencyRecord, CRMLead, LeadSource, LeadStageHistory
from apps.crm.repositories.crm_repository import CRMRepository
from apps.crm.services.v1.crm_service import CRMService
from apps.crm.tests.helpers import lead_student, stage
from apps.users.models import User
from core.permissions import Role
from core.role_principals import RolePrincipal

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_identical_lead_creates_execute_domain_side_effect_once(tenant_a, user_in):
    manager = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT])
    with schema_context(tenant_a.schema_name):
        branch = manager.role_memberships.get(revoked_at__isnull=True).branch
        student = lead_student(branch=branch)
        stage_id = stage("new").pk
        source_id = LeadSource.objects.get(slug="other").pk
        actor_id = manager.pk
        principal_id = manager.test_principal_id
    barrier = Barrier(2)

    def run():
        close_old_connections()
        try:
            with schema_context(tenant_a.schema_name):
                actor = User.objects.get(pk=actor_id)
                barrier.wait(timeout=10)
                return CRMService(CRMRepository()).create_lead(
                    LeadCreateDTO(
                        student_id=student.pk,
                        stage_id=stage_id,
                        source_id=source_id,
                    ),
                    scope=CRMScope(branch_wide_ids=frozenset({branch.pk})),
                    actor=actor,
                    actor_principal=RolePrincipal(
                        kind="staff",
                        principal_id=principal_id,
                        user_id=actor_id,
                    ),
                    idempotency_key="concurrent-lead-create-0001",
                )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: run(), range(2)))

    assert len({lead.pk for lead, _replayed in results}) == 1
    assert sorted(replayed for _lead, replayed in results) == [False, True]
    with schema_context(tenant_a.schema_name):
        assert CRMLead.objects.filter(student_id=student.pk).count() == 1
        assert LeadStageHistory.objects.filter(lead__student_id=student.pk).count() == 1
        assert (
            CRMIdempotencyRecord.objects.filter(
                actor_principal_kind="staff",
                actor_principal_id=principal_id,
            ).count()
            == 1
        )
