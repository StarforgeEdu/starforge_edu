from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.crm.models import CRMIdempotencyRecord


class Command(BaseCommand):
    help = "Delete expired CRM idempotency receipts after their documented replay window."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--schema",
            action="append",
            dest="schema_names",
            help="Limit processing to this tenant schema (repeatable).",
        )
        parser.add_argument("--retention-days", type=int, default=30)
        parser.add_argument("--batch-size", type=int, default=5000)

    def handle(self, *args, **options):
        retention_days = options["retention_days"]
        batch_size = options["batch_size"]
        if not 7 <= retention_days <= 3650:
            raise CommandError("--retention-days must be between 7 and 3650")
        if not 1 <= batch_size <= 50_000:
            raise CommandError("--batch-size must be between 1 and 50000")
        public = get_public_schema_name()
        Tenant = get_tenant_model()
        with schema_context(public):
            known = set(Tenant.objects.exclude(schema_name=public).values_list("schema_name", flat=True))
        requested = options.get("schema_names")
        if requested:
            unknown = set(requested) - known
            if unknown:
                raise CommandError(f"Unknown tenant schema(s): {', '.join(sorted(unknown))}")
            schemas = list(dict.fromkeys(requested))
        else:
            schemas = sorted(known)

        cutoff = timezone.now() - timedelta(days=retention_days)
        deleted = 0
        for schema_name in schemas:
            schema_deleted = 0
            with schema_context(schema_name):
                while True:
                    ids = list(
                        CRMIdempotencyRecord.objects.filter(created_at__lt=cutoff)
                        .order_by("created_at", "pk")
                        .values_list("pk", flat=True)[:batch_size]
                    )
                    if not ids:
                        break
                    batch_deleted, _detail = CRMIdempotencyRecord.objects.filter(pk__in=ids).delete()
                    schema_deleted += batch_deleted
            deleted += schema_deleted
            self.stdout.write(f"{schema_name}: purged {schema_deleted} receipt(s)")
        self.stdout.write(
            self.style.SUCCESS(
                f"Purged {deleted} CRM idempotency receipt(s) older than {retention_days} days."
            )
        )
