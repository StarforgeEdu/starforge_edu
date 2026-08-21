"""Read-only inventory for ambiguous physical-print attempts."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context


class Command(BaseCommand):
    help = "Report identifier-free stale/reconciliation-required print-job counts for one tenant."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--schema", required=True)
        parser.add_argument(
            "--fail-on-open-reconciliation",
            action="store_true",
            help="Exit non-zero when stale or reconciliation-required attempts exist.",
        )

    def handle(self, *args, **options) -> None:
        schema_name = options["schema"]
        public = get_public_schema_name()
        Tenant = get_tenant_model()
        with schema_context(public):
            known = (
                schema_name != public
                and Tenant.objects.filter(schema_name=schema_name, is_active=True).exists()
            )
        if not known:
            raise CommandError("The selected active tenant schema does not exist.")

        from apps.printing.services import print_reconciliation_inventory

        with schema_context(schema_name):
            inventory = print_reconciliation_inventory()
        self.stdout.write(json.dumps(inventory, sort_keys=True))
        if options["fail_on_open_reconciliation"] and any(inventory.values()):
            raise CommandError("Physical-print reconciliation is not clear.")
