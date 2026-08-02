"""Crash-window and reconciliation tests for external notification delivery."""

from __future__ import annotations

from datetime import timedelta
from itertools import count

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db
_RECIPIENT_SEQUENCE = count(1_000)


def _notification(tenant):
    from apps.notifications.models import Notification
    from apps.notifications.tests.helpers import ensure_notification_principal
    from apps.users.tests.factories import UserFactory

    sequence = next(_RECIPIENT_SEQUENCE)
    with schema_context(tenant.schema_name):
        user = ensure_notification_principal(
            UserFactory(
                phone=f"+99890{sequence:07d}",
                email=f"recipient-{sequence}@example.com",
            )
        )
        return Notification.objects.create(
            user=user,
            event_type="attendance.absent",
            title="Absent",
            body="A learner is absent.",
        )


def test_provider_claim_schema_has_bounded_sweep_index_and_active_uniqueness():
    from apps.notifications.models import NotificationDelivery

    indexes = {index.name: index for index in NotificationDelivery._meta.indexes}
    assert indexes["notif_delivery_status_created_idx"].fields == ["status", "created_at"]
    constraints = {constraint.name: constraint for constraint in NotificationDelivery._meta.constraints}
    claim_constraint = constraints["notif_one_provider_contact_per_destination"]
    assert claim_constraint.fields == ("notification", "channel", "delivery_key")
    assert claim_constraint.condition is not None


def test_active_claim_states_are_unique_but_confirmed_not_sent_releases_key(tenant_a):
    from apps.notifications.models import Channel, NotificationDelivery

    notification = _notification(tenant_a)
    with schema_context(tenant_a.schema_name):
        first = NotificationDelivery.objects.create(
            notification=notification,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.CLAIMED,
            delivery_key="recipient",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationDelivery.objects.create(
                notification=notification,
                channel=Channel.SMS,
                status=NotificationDelivery.Status.UNKNOWN,
                delivery_key="recipient",
            )

        first.status = NotificationDelivery.Status.FAILED
        first.save(update_fields=["status"])
        replacement = NotificationDelivery.objects.create(
            notification=notification,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.CLAIMED,
            delivery_key="recipient",
        )
        assert replacement.pk != first.pk


def test_worker_death_after_provider_acceptance_never_contacts_provider_twice(tenant_a, monkeypatch):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, NotificationDelivery

    class SimulatedWorkerDeath(BaseException):
        pass

    notification = _notification(tenant_a)
    provider_calls: list[str] = []

    class Provider:
        def send(self, *, phone, text):
            provider_calls.append(phone)
            return {"status": "accepted", "message_id": "provider-accepted"}

    monkeypatch.setattr("infrastructure.sms.eskiz_client.get_sms_client", lambda: Provider())
    monkeypatch.setattr(
        nt,
        "_complete_provider_delivery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SimulatedWorkerDeath()),
    )

    with schema_context(tenant_a.schema_name):
        with pytest.raises(SimulatedWorkerDeath):
            nt._deliver_sms(notification, "A learner is absent.")

        claim = NotificationDelivery.objects.get(
            notification=notification,
            channel=Channel.SMS,
            delivery_key="recipient",
        )
        assert claim.status == NotificationDelivery.Status.CLAIMED
        assert nt._deliver_sms(notification, "A learner is absent.") == "already_claimed"

        NotificationDelivery.objects.filter(pk=claim.pk).update(
            created_at=timezone.now() - nt.PROVIDER_CLAIM_STALE_AFTER - timedelta(seconds=1)
        )
        assert nt.reconcile_stale_provider_delivery_claims_for_schema.run() == 1
        claim.refresh_from_db()
        assert claim.status == NotificationDelivery.Status.UNKNOWN
        assert claim.provider_response["unknown_reason"] == "stale_claim"
        assert claim.provider_response["reconciliation_required"] is True
        assert nt._deliver_sms(notification, "A learner is absent.") == "already_claimed"

    assert provider_calls == [notification.user.phone]


def test_provider_timeout_becomes_unknown_and_is_not_automatically_retried(tenant_a, monkeypatch):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, NotificationDelivery

    notification = _notification(tenant_a)
    calls = 0

    class Provider:
        def send(self, *, phone, text):
            nonlocal calls
            calls += 1
            raise TimeoutError("ambiguous provider timeout")

    monkeypatch.setattr("infrastructure.sms.eskiz_client.get_sms_client", lambda: Provider())
    with schema_context(tenant_a.schema_name):
        with pytest.raises(nt.ProviderOutcomeUnknown):
            nt._deliver_sms(notification, "A learner is absent.")
        assert nt._deliver_sms(notification, "A learner is absent.") == "already_claimed"
        delivery = NotificationDelivery.objects.get(
            notification=notification,
            channel=Channel.SMS,
            delivery_key="recipient",
        )
        assert delivery.status == NotificationDelivery.Status.UNKNOWN
        assert delivery.provider_response["error"] == "TimeoutError"
        assert "ambiguous provider timeout" not in str(delivery.provider_response)
    assert calls == 1


def test_stale_claim_sweep_leaves_fresh_and_terminal_rows_unchanged(tenant_a):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, NotificationDelivery

    stale_notification = _notification(tenant_a)
    fresh_notification = _notification(tenant_a)
    sent_notification = _notification(tenant_a)
    with schema_context(tenant_a.schema_name):
        stale = NotificationDelivery.objects.create(
            notification=stale_notification,
            channel=Channel.EMAIL,
            status=NotificationDelivery.Status.CLAIMED,
            delivery_key="recipient",
            provider_response={"claimed_at": timezone.now().isoformat()},
        )
        fresh = NotificationDelivery.objects.create(
            notification=fresh_notification,
            channel=Channel.EMAIL,
            status=NotificationDelivery.Status.CLAIMED,
            delivery_key="recipient",
        )
        sent = NotificationDelivery.objects.create(
            notification=sent_notification,
            channel=Channel.EMAIL,
            status=NotificationDelivery.Status.SENT,
            delivery_key="recipient",
        )
        NotificationDelivery.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - nt.PROVIDER_CLAIM_STALE_AFTER - timedelta(seconds=1)
        )

        assert nt.reconcile_stale_provider_delivery_claims_for_schema.run() == 1
        stale.refresh_from_db()
        fresh.refresh_from_db()
        sent.refresh_from_db()
        assert stale.status == NotificationDelivery.Status.UNKNOWN
        assert fresh.status == NotificationDelivery.Status.CLAIMED
        assert sent.status == NotificationDelivery.Status.SENT


def test_only_confirmed_not_sent_reconciliation_can_queue_one_retry(
    tenant_a,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, NotificationDelivery
    from apps.notifications.services.delivery_reconciliation import (
        DeliveryReconciliationError,
        reconcile_unknown_delivery,
    )
    from celery_tasks.notification_tasks import deliver_single_channel

    notification = _notification(tenant_a)
    queued: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        deliver_single_channel,
        "delay",
        lambda *args, **kwargs: queued.append((args, kwargs)),
    )
    with schema_context(tenant_a.schema_name):
        claim = NotificationDelivery.objects.create(
            notification=notification,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.UNKNOWN,
            delivery_key="recipient",
            provider_response={"reconciliation_required": True},
        )
        with pytest.raises(DeliveryReconciliationError, match="only a confirmed not-sent"):
            reconcile_unknown_delivery(
                delivery_id=claim.pk,
                outcome="sent",
                reference="provider-receipt-1",
                operator="ops@example.com",
                retry=True,
            )

        with django_capture_on_commit_callbacks(execute=True):
            resolved = reconcile_unknown_delivery(
                delivery_id=claim.pk,
                outcome="not_sent",
                reference="provider-ticket-42",
                operator="ops@example.com",
                retry=True,
            )
        assert resolved.status == NotificationDelivery.Status.FAILED
        assert resolved.provider_response["retryable"] is True
        assert resolved.provider_response["reconciliation_reference"] == "provider-ticket-42"
        assert queued == [
            (
                (notification.pk, Channel.SMS),
                {"_schema_name": tenant_a.schema_name},
            )
        ]
        # A broker loss after the on-commit publish cannot lose the operator's
        # authorized retry: the periodic reconciler re-enqueues the durable
        # marker once its short lease expires.
        resolved.provider_response["retry_last_enqueued_at"] = (
            timezone.now() - nt.RECONCILED_RETRY_LEASE - timedelta(seconds=1)
        ).isoformat()
        resolved.save(update_fields=["provider_response"])
        with django_capture_on_commit_callbacks(execute=True):
            assert nt._enqueue_reconciled_provider_retries() == 1
        assert len(queued) == 2

        replacement = NotificationDelivery.objects.create(
            notification=notification,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.CLAIMED,
            delivery_key="recipient",
        )
        assert replacement.pk != claim.pk


def test_reconciled_sent_claim_is_terminal_and_cannot_be_reopened(tenant_a):
    from apps.notifications.models import Channel, NotificationDelivery
    from apps.notifications.services.delivery_reconciliation import (
        DeliveryReconciliationError,
        reconcile_unknown_delivery,
    )

    notification = _notification(tenant_a)
    with schema_context(tenant_a.schema_name):
        claim = NotificationDelivery.objects.create(
            notification=notification,
            channel=Channel.EMAIL,
            status=NotificationDelivery.Status.UNKNOWN,
            delivery_key="recipient",
        )
        resolved = reconcile_unknown_delivery(
            delivery_id=claim.pk,
            outcome="sent",
            reference="receipt-accepted-99",
            operator="ops@example.com",
        )
        assert resolved.status == NotificationDelivery.Status.SENT
        assert resolved.sent_at is not None
        with pytest.raises(DeliveryReconciliationError, match="only an unknown"):
            reconcile_unknown_delivery(
                delivery_id=claim.pk,
                outcome="not_sent",
                reference="contradictory-ticket",
                operator="ops@example.com",
            )
