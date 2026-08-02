"""Regression coverage for asynchronous, recoverable campaign delivery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from django.db import close_old_connections
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

CAMPAIGNS = "/api/v1/campaigns/"


def _branch(tenant):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        return BranchFactory.create()


def _student(branch, *, parent=None):
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    student = StudentProfileFactory.create(branch=branch)
    parent = parent or ParentProfileFactory.create()
    GuardianFactory.create(parent=parent, student=student, is_primary=True)
    return student


def _create(client, branch) -> int:
    response = client.post(
        CAMPAIGNS,
        {"name": "Reliable", "message": "hello", "branch": branch.pk},
        format="json",
    )
    assert response.status_code == 201
    return response.json()["data"]["id"]


def test_send_endpoint_claims_and_enqueues_without_provider_io(
    tenant_a,
    user_in,
    as_user,
    sms_outbox,
    monkeypatch,
):
    from apps.campaigns.models import Campaign
    from celery_tasks.campaign_tasks import deliver_campaign

    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        _student(branch)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    campaign_id = _create(client, branch)

    queued: list[tuple[int, str, str]] = []

    def _record_delay(pk, token, *, _schema_name):
        queued.append((pk, token, _schema_name))

    monkeypatch.setattr(deliver_campaign, "delay", _record_delay)

    first = client.post(f"{CAMPAIGNS}{campaign_id}/send/", {}, format="json")
    second = client.post(f"{CAMPAIGNS}{campaign_id}/send/", {}, format="json")

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["data"]["status"] == Campaign.Status.SENDING
    assert len(sms_outbox) == 0
    assert len(queued) == 1
    assert queued[0][0] == campaign_id
    assert queued[0][2] == tenant_a.schema_name
    with schema_context(tenant_a.schema_name):
        campaign = Campaign.objects.get(pk=campaign_id)
        assert str(campaign.send_claim_token) == queued[0][1]
        assert campaign.send_attempts == 1


def test_broker_failure_is_durable_and_republished_by_dispatcher(
    tenant_a,
    user_in,
    as_user,
    monkeypatch,
):
    from apps.campaigns.models import Campaign
    from apps.campaigns.services import dispatch_due_campaigns, send_campaign
    from celery_tasks.campaign_tasks import deliver_campaign

    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        _student(branch)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    campaign_id = _create(client, branch)

    def _queue_down(*args, **kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(deliver_campaign, "delay", _queue_down)
    with schema_context(tenant_a.schema_name):
        campaign = send_campaign(campaign_id=campaign_id)
        campaign.refresh_from_db()
        first_token = campaign.send_claim_token
        assert campaign.status == Campaign.Status.SENDING
        assert campaign.send_heartbeat_at is None
        assert campaign.last_error == "queue_delivery_failed"

        queued = []
        monkeypatch.setattr(deliver_campaign, "delay", lambda *args, **kwargs: queued.append((args, kwargs)))
        assert dispatch_due_campaigns() == 1

        campaign.refresh_from_db()
        assert campaign.send_claim_token != first_token
        assert campaign.send_heartbeat_at is not None
        assert campaign.send_attempts == 2
        assert campaign.last_error == ""
        assert len(queued) == 1


def test_stale_lease_is_reclaimed_and_old_task_is_fenced(
    tenant_a,
    user_in,
    as_user,
    sms_outbox,
    monkeypatch,
):
    from apps.campaigns.models import Campaign
    from apps.campaigns.services import (
        _CAMPAIGN_SEND_LEASE,
        dispatch_due_campaigns,
        process_campaign_delivery,
        send_campaign,
    )
    from celery_tasks.campaign_tasks import deliver_campaign

    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        _student(branch)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    campaign_id = _create(client, branch)
    queued = []
    monkeypatch.setattr(deliver_campaign, "delay", lambda *args, **kwargs: queued.append((args, kwargs)))

    with schema_context(tenant_a.schema_name):
        campaign = send_campaign(campaign_id=campaign_id)
        old_token = str(campaign.send_claim_token)
        Campaign.objects.filter(pk=campaign_id).update(
            send_heartbeat_at=timezone.now() - _CAMPAIGN_SEND_LEASE - timedelta(seconds=1)
        )

        assert dispatch_due_campaigns() == 1
        campaign.refresh_from_db()
        new_token = str(campaign.send_claim_token)
        assert new_token != old_token
        assert campaign.send_attempts == 2

        assert process_campaign_delivery(campaign_id=campaign_id, claim_token=old_token) is None
        assert len(sms_outbox) == 0
        assert (
            process_campaign_delivery(campaign_id=campaign_id, claim_token=new_token) == Campaign.Status.SENT
        )
        assert len(sms_outbox) == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_workers_are_serialized_per_campaign(
    tenant_a,
    sms_outbox,
    monkeypatch,
):
    """Two task copies cannot separately text siblings sharing one guardian phone."""
    from apps.campaigns.models import Campaign, CampaignRecipient
    from apps.campaigns.services import create_campaign, process_campaign_delivery, send_campaign
    from apps.org.tests.factories import BranchFactory
    from apps.parents.tests.factories import ParentProfileFactory
    from celery_tasks.campaign_tasks import deliver_campaign

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        parent = ParentProfileFactory.create()
        students = [_student(branch, parent=parent), _student(branch, parent=parent)]
        campaign = create_campaign(
            name="Concurrent",
            message="one text",
            segment=None,
            created_by=None,
            branch=branch,
        )
        monkeypatch.setattr(deliver_campaign, "delay", lambda *args, **kwargs: None)
        campaign = send_campaign(campaign_id=campaign.pk)
        campaign_id = campaign.pk
        claim_token = str(campaign.send_claim_token)
        branch_id = branch.pk
        parent_id = parent.pk
        student_ids = [student.pk for student in students]
        user_ids = [parent.user_id, *(student.user_id for student in students)]

    entered = Event()
    release = Event()

    class _SlowClient:
        def send(self, *, phone, text):
            sms_outbox.append({"phone": phone, "text": text})
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test timed out waiting to release provider")

    monkeypatch.setattr("apps.campaigns.services.get_sms_client", lambda: _SlowClient())

    def _deliver():
        close_old_connections()
        try:
            with schema_context(tenant_a.schema_name):
                return process_campaign_delivery(
                    campaign_id=campaign_id,
                    claim_token=claim_token,
                )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_deliver)
        try:
            assert entered.wait(timeout=10)
            duplicate = pool.submit(_deliver)
            assert duplicate.result(timeout=10) is None
        finally:
            release.set()
        assert first.result(timeout=10) == Campaign.Status.SENT

    with schema_context(tenant_a.schema_name):
        campaign = Campaign.objects.get(pk=campaign_id)
        assert campaign.status == Campaign.Status.SENT
        assert campaign.recipients.filter(status=CampaignRecipient.Status.SENT).count() == 2
        # transaction=True commits tenant rows; pytest only flushes the public schema.
        # Remove this test's graph explicitly so later campaign audience counts and
        # factory get_or_create sequences cannot see it.
        from apps.org.models import Branch
        from apps.parents.models import ParentProfile
        from apps.students.models import StudentProfile
        from apps.users.models import User

        campaign.delete()
        StudentProfile.objects.filter(pk__in=student_ids).delete()
        ParentProfile.objects.filter(pk=parent_id).delete()
        User.objects.filter(pk__in=user_ids).delete()
        Branch.objects.filter(pk=branch_id).delete()
    assert len(sms_outbox) == 1


def test_delivery_task_uses_crash_safe_acknowledgement():
    from celery_tasks.campaign_tasks import deliver_campaign

    assert deliver_campaign.acks_late is True
    assert deliver_campaign.reject_on_worker_lost is True
    assert deliver_campaign.max_retries == 3
