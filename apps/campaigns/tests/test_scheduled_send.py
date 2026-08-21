"""F10-1 dynamic send date: a campaign can be scheduled for a future time and is
auto-sent by the beat sweep (dispatch_due_campaigns) once that time arrives — no manual
send required. A campaign with no scheduled_at is manual-only (unchanged behaviour)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

CAMPAIGNS = "/api/v1/campaigns/"


def _student(branch):
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    student = StudentProfileFactory.create(branch=branch)
    parent = ParentProfileFactory.create()
    GuardianFactory.create(parent=parent, student=student, is_primary=True)
    return student


def _branch(tenant):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        return BranchFactory.create()


def _dispatch(tenant):
    from apps.campaigns.services import dispatch_due_campaigns

    with schema_context(tenant.schema_name):
        return dispatch_due_campaigns()


def test_scheduled_campaign_is_created_draft_and_not_sent_until_due(tenant_a, user_in, as_user, sms_outbox):
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        _student(branch)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))

    future = (timezone.now() + timedelta(hours=2)).isoformat()
    created = client.post(
        CAMPAIGNS,
        {"name": "Later", "message": "See you Monday", "branch": branch.id, "scheduled_at": future},
        format="json",
    )
    assert created.status_code == 201, created.content
    data = created.json()["data"]
    assert data["status"] == "draft"
    assert data["scheduled_at"] is not None

    # Not due yet -> the sweep ignores it, nothing is texted.
    assert _dispatch(tenant_a) == 0
    assert len(sms_outbox) == 0
    with schema_context(tenant_a.schema_name):
        from apps.campaigns.models import Campaign

        assert Campaign.objects.get(pk=data["id"]).status == Campaign.Status.DRAFT


def test_due_scheduled_campaign_is_auto_sent_by_the_sweep(tenant_a, user_in, as_user, sms_outbox):
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        _student(branch)
        _student(branch)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))

    past = (timezone.now() - timedelta(minutes=1)).isoformat()
    cid = client.post(
        CAMPAIGNS,
        {"name": "Due now", "message": "Class today", "branch": branch.id, "scheduled_at": past},
        format="json",
    ).json()["data"]["id"]

    dispatched = _dispatch(tenant_a)
    assert dispatched == 1
    assert len(sms_outbox) == 2
    with schema_context(tenant_a.schema_name):
        from apps.campaigns.models import Campaign

        campaign = Campaign.objects.get(pk=cid)
        assert campaign.status == Campaign.Status.SENT
        assert campaign.sent_count == 2

    # Idempotent: a second sweep does not re-send an already-sent campaign.
    assert _dispatch(tenant_a) == 0
    assert len(sms_outbox) == 2


def test_unscheduled_campaign_is_ignored_by_the_sweep(tenant_a, user_in, as_user, sms_outbox):
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        _student(branch)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    cid = client.post(
        CAMPAIGNS, {"name": "Manual", "message": "hi", "branch": branch.id}, format="json"
    ).json()["data"]["id"]

    # No scheduled_at -> a manual draft; the sweep must never touch it.
    assert _dispatch(tenant_a) == 0
    assert len(sms_outbox) == 0
    with schema_context(tenant_a.schema_name):
        from apps.campaigns.models import Campaign

        assert Campaign.objects.get(pk=cid).scheduled_at is None


def test_invalid_scheduled_at_is_400(tenant_a, user_in, as_user):
    branch = _branch(tenant_a)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    resp = client.post(
        CAMPAIGNS,
        {"name": "x", "message": "hi", "branch": branch.id, "scheduled_at": "not-a-datetime"},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_concurrent_send_does_not_double_text_recipients(tenant_a, user_in, as_user, sms_outbox, monkeypatch):
    """Regression (self-review, HIGH double-send): the send loop runs outside the campaign
    row lock and SENDING is resumable, so the scheduled-dispatch beat can race a manual
    'send now' (or a redelivered task) on the same campaign. The per-recipient
    compare-and-swap claim must guarantee each recipient's SMS is sent at most once.

    Deterministic simulation of the race: the SMS client, on its first send, flips every
    other still-PENDING recipient to SENT (as a concurrent worker would by claiming them);
    the loop's own CAS for those rows then affects 0 rows and skips — so they are NOT
    texted a second time."""
    from apps.campaigns.models import CampaignRecipient

    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        _student(branch)
        _student(branch)
        _student(branch)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    cid = client.post(
        CAMPAIGNS, {"name": "Race", "message": "hi", "branch": branch.id}, format="json"
    ).json()["data"]["id"]

    class _RacingClient:
        raced = False

        def send(self, *, phone, text):
            if not _RacingClient.raced:
                _RacingClient.raced = True
                # A concurrent worker claims all remaining PENDING recipients first.
                with schema_context(tenant_a.schema_name):
                    CampaignRecipient.objects.filter(
                        campaign_id=cid, status=CampaignRecipient.Status.PENDING
                    ).update(status=CampaignRecipient.Status.SENT, sent_at=timezone.now())
            sms_outbox.append({"phone": phone, "text": text})

    monkeypatch.setattr("apps.campaigns.services.get_sms_client", lambda: _RacingClient())
    resp = client.post(f"{CAMPAIGNS}{cid}/send/", {}, format="json")
    assert resp.status_code == 202
    # Only the FIRST recipient was actually texted; the two the "concurrent worker" claimed
    # were skipped by the CAS, never double-sent.
    assert len(sms_outbox) == 1


def test_scheduled_campaign_can_still_be_sent_manually(tenant_a, user_in, as_user, sms_outbox):
    """Setting a schedule does not block the manual send endpoint — a human can send it
    early; the later sweep then finds it already SENT and skips it."""
    branch = _branch(tenant_a)
    with schema_context(tenant_a.schema_name):
        _student(branch)
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    future = (timezone.now() + timedelta(hours=5)).isoformat()
    cid = client.post(
        CAMPAIGNS,
        {"name": "Early", "message": "hi", "branch": branch.id, "scheduled_at": future},
        format="json",
    ).json()["data"]["id"]

    sent = client.post(f"{CAMPAIGNS}{cid}/send/", {}, format="json")
    assert sent.status_code == 202
    assert sent.json()["data"]["status"] == "sent"
    assert len(sms_outbox) == 1
    assert _dispatch(tenant_a) == 0  # sweep skips the already-sent campaign
