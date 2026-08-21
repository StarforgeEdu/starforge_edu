"""Evidence-backed resolution of indeterminate external delivery claims."""

from __future__ import annotations

import re
from typing import Literal

from django.db import transaction
from django.utils import timezone

from apps.notifications.models import Channel, NotificationDelivery
from core.utils import current_schema

ReconciliationOutcome = Literal["sent", "not_sent"]
EXTERNAL_CHANNELS = frozenset((Channel.SMS, Channel.EMAIL, Channel.PUSH))


class DeliveryReconciliationError(ValueError):
    """The requested claim transition is unsafe or invalid."""


def reconcile_unknown_delivery(
    *,
    delivery_id: int,
    outcome: ReconciliationOutcome,
    reference: str,
    operator: str,
    retry: bool = False,
) -> NotificationDelivery:
    """Resolve one UNKNOWN claim using a provider receipt or operator ticket.

    ``not_sent`` may be explicitly retried because the supplied evidence proves
    that another provider contact cannot duplicate the original attempt.
    ``sent`` is terminal. CLAIMED rows must first age through the stale-claim
    sweep so an in-flight worker can never be overridden by an operator race.
    """

    clean_reference = _validated_audit_identifier(reference, field="reference")
    clean_operator = _validated_audit_identifier(operator, field="operator")
    if outcome not in ("sent", "not_sent"):
        raise DeliveryReconciliationError("outcome must be 'sent' or 'not_sent'")
    if retry and outcome != "not_sent":
        raise DeliveryReconciliationError("only a confirmed not-sent delivery may be retried")

    with transaction.atomic():
        try:
            delivery = NotificationDelivery.objects.select_for_update().get(pk=delivery_id)
        except NotificationDelivery.DoesNotExist as exc:
            raise DeliveryReconciliationError("delivery claim does not exist") from exc
        if delivery.channel not in EXTERNAL_CHANNELS or not delivery.delivery_key:
            raise DeliveryReconciliationError("delivery is not an external provider claim")
        if delivery.status != NotificationDelivery.Status.UNKNOWN:
            raise DeliveryReconciliationError("only an unknown delivery claim may be reconciled")

        reconciled_at = timezone.now()
        evidence = dict(delivery.provider_response or {})
        evidence.update(
            {
                "reconciled_at": reconciled_at.isoformat(),
                "reconciliation_outcome": outcome,
                "reconciliation_reference": clean_reference,
                "reconciled_by": clean_operator,
                "reconciliation_required": False,
                "retryable": retry,
            }
        )
        if outcome == "sent":
            delivery.status = NotificationDelivery.Status.SENT
            delivery.sent_at = reconciled_at
        else:
            delivery.status = NotificationDelivery.Status.FAILED
            delivery.sent_at = None
            if retry:
                evidence["retry_requested_at"] = reconciled_at.isoformat()
                evidence["retry_last_enqueued_at"] = reconciled_at.isoformat()
        delivery.provider_response = evidence
        delivery.save(update_fields=["status", "sent_at", "provider_response"])

        if retry:
            notification_id = delivery.notification_id
            channel = delivery.channel
            schema = current_schema()

            def enqueue_retry() -> None:
                from celery_tasks.notification_tasks import deliver_single_channel

                deliver_single_channel.delay(
                    notification_id,
                    channel,
                    _schema_name=schema,
                )

            transaction.on_commit(enqueue_retry)
        return delivery


def _validated_audit_identifier(value: str, *, field: str) -> str:
    identifier = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}", identifier):
        raise DeliveryReconciliationError(f"{field} must be a 1 to 128 character audit identifier")
    return identifier
