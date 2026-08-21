"""Resolve one provider delivery whose outcome is unknown."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.notifications.services.delivery_reconciliation import (
    DeliveryReconciliationError,
    reconcile_unknown_delivery,
)


class Command(BaseCommand):
    help = (
        "Resolve one unknown notification provider claim from reviewed evidence. "
        "No retry occurs unless --outcome not_sent and --retry are both supplied."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--schema", required=True, help="Exact active tenant schema.")
        parser.add_argument("--delivery-id", required=True, type=int)
        parser.add_argument("--outcome", required=True, choices=("sent", "not_sent"))
        parser.add_argument("--reference", required=True, help="Provider receipt or review ticket ID.")
        parser.add_argument("--operator", required=True, help="Auditable operator account identifier.")
        parser.add_argument(
            "--retry",
            action="store_true",
            help="Retry only after evidence confirms the provider did not send.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        schema_name = str(options["schema"])
        public_schema = get_public_schema_name()
        with schema_context(public_schema):
            tenant_is_active = (
                get_tenant_model()
                .objects.filter(
                    schema_name=schema_name,
                    is_active=True,
                )
                .exists()
            )
        if schema_name == public_schema or not tenant_is_active:
            raise CommandError("--schema must identify an active tenant")
        try:
            with schema_context(schema_name):
                delivery = reconcile_unknown_delivery(
                    delivery_id=options["delivery_id"],
                    outcome=options["outcome"],
                    reference=options["reference"],
                    operator=options["operator"],
                    retry=bool(options["retry"]),
                )
        except DeliveryReconciliationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "channel": delivery.channel,
                    "delivery_id": delivery.pk,
                    "outcome": options["outcome"],
                    "retry_queued": bool(options["retry"]),
                    "schema": schema_name,
                    "status": delivery.status,
                },
                sort_keys=True,
            )
        )
