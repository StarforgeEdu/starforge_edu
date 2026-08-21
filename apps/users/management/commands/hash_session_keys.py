"""Irreversibly replace legacy plaintext session keys with SHA-256 digests."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.users.models import Session
from core.session_auth import hash_session_key

_HASH_PREFIX = "sha256$"


class Command(BaseCommand):
    help = (
        "Hash legacy plaintext Session keys in every schema. Run only after all "
        "application nodes use the hash-aware session authenticator."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--schema",
            action="append",
            dest="schema_names",
            help="Limit processing to this schema (repeatable). Defaults to every schema.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Rows committed per transaction (default: 1000).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count legacy rows without changing them.",
        )

    def handle(self, *args, **options) -> None:
        batch_size = options["batch_size"]
        if batch_size < 1 or batch_size > 10_000:
            raise CommandError("--batch-size must be between 1 and 10000")

        public_schema = get_public_schema_name()
        Tenant = get_tenant_model()
        with schema_context(public_schema):
            known_schemas = {
                public_schema,
                *Tenant.objects.values_list("schema_name", flat=True),
            }
        requested = options.get("schema_names")
        if requested:
            unknown = set(requested) - known_schemas
            if unknown:
                raise CommandError(f"Unknown schema(s): {', '.join(sorted(unknown))}")
            schema_names = list(dict.fromkeys(requested))
        else:
            schema_names = sorted(known_schemas)

        dry_run = options["dry_run"]
        total = 0
        for schema_name in schema_names:
            with schema_context(schema_name):
                legacy = Session.objects.exclude(key_hash__startswith=_HASH_PREFIX)
                if dry_run:
                    count = legacy.count()
                    total += count
                    self.stdout.write(f"{schema_name}: {count} legacy session key(s)")
                    continue

                schema_total = 0
                while True:
                    batch = list(legacy.order_by("pk").values_list("pk", "key_hash")[:batch_size])
                    if not batch:
                        break
                    # Conditional updates make the command safe alongside normal
                    # lazy upgrades and make a partial/retried run idempotent.
                    with transaction.atomic():
                        for session_id, raw_key in batch:
                            updated = Session.objects.filter(
                                pk=session_id,
                                key_hash=raw_key,
                            ).update(key_hash=hash_session_key(raw_key))
                            schema_total += updated
                total += schema_total
                self.stdout.write(f"{schema_name}: hashed {schema_total} session key(s)")

        action = "would hash" if dry_run else "hashed"
        self.stdout.write(self.style.SUCCESS(f"{action} {total} legacy session key(s) total"))
