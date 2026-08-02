"""Read-only release gate for AI attribution, scope, and privacy retention."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context


class Command(BaseCommand):
    help = "Report unresolved AI ownership and expired sensitive content across tenant schemas."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--schema", action="append", dest="schemas")
        parser.add_argument("--fail-on-unresolved", action="store_true")
        parser.add_argument("--fail-on-expired-content", action="store_true")
        parser.add_argument("--fail-on-uncertain-provider-outcome", action="store_true")

    def handle(self, *args, **options) -> None:
        requested = options.get("schemas")
        public = get_public_schema_name()
        Tenant = get_tenant_model()
        with schema_context(public):
            known = set(Tenant.objects.exclude(schema_name=public).values_list("schema_name", flat=True))
        if requested:
            unknown = set(requested) - known
            if unknown:
                raise CommandError(f"Unknown tenant schema count: {len(unknown)}")
            schemas = list(dict.fromkeys(requested))
        else:
            schemas = sorted(known)

        unresolved_total = 0
        expired_content_total = 0
        uncertain_total = 0
        uncertain_reserved_total = 0
        now = timezone.now()
        for schema_name in schemas:
            with schema_context(schema_name):
                from apps.ai.models import AIRequest

                unresolved = AIRequest.objects.filter(
                    Q(attribution_status=AIRequest.AttributionStatus.UNRESOLVED)
                    | Q(scope_status=AIRequest.ScopeStatus.UNRESOLVED)
                ).count()
                expired_content = (
                    AIRequest.objects.filter(
                        content_expires_at__lte=now,
                        content_purged_at__isnull=True,
                    )
                    .filter(~Q(output_ciphertext="") | ~Q(redaction_map=""))
                    .count()
                )
                uncertain = AIRequest.objects.filter(status=AIRequest.Status.UNCERTAIN).aggregate(
                    count=Count("pk"),
                    reserved_tokens=Sum("reserved_tokens", default=0),
                )
                report = {
                    "schema": schema_name,
                    "resolved": AIRequest.objects.filter(
                        attribution_status=AIRequest.AttributionStatus.RESOLVED,
                    )
                    .exclude(scope_status=AIRequest.ScopeStatus.UNRESOLVED)
                    .count(),
                    "unresolved": unresolved,
                    "expired_sensitive_content": expired_content,
                    "uncertain_provider_outcomes": int(uncertain["count"] or 0),
                    "uncertain_reserved_tokens": int(uncertain["reserved_tokens"] or 0),
                }
            unresolved_total += unresolved
            expired_content_total += expired_content
            uncertain_total += report["uncertain_provider_outcomes"]
            uncertain_reserved_total += report["uncertain_reserved_tokens"]
            self.stdout.write(json.dumps(report, sort_keys=True))

        summary = {
            "schemas": len(schemas),
            "unresolved": unresolved_total,
            "expired_sensitive_content": expired_content_total,
            "uncertain_provider_outcomes": uncertain_total,
            "uncertain_reserved_tokens": uncertain_reserved_total,
        }
        self.stdout.write(json.dumps(summary, sort_keys=True))
        failures: list[str] = []
        if options["fail_on_unresolved"] and unresolved_total:
            failures.append("unresolved attribution")
        if options["fail_on_expired_content"] and expired_content_total:
            failures.append("expired sensitive content")
        if options["fail_on_uncertain_provider_outcome"] and uncertain_total:
            failures.append("uncertain provider outcome")
        if failures:
            raise CommandError(f"AI release review failed: {', '.join(failures)}")
