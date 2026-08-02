"""Explicit operator workflow for ambiguous paid-provider attempts."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context


class Command(BaseCommand):
    help = (
        "Reconcile one quarantined AI provider attempt from reviewed billing evidence; "
        "this never replays model work."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--schema", required=True)
        parser.add_argument("--request-id", required=True, type=int)
        parser.add_argument(
            "--outcome",
            required=True,
            choices=("not_charged", "charged"),
        )
        parser.add_argument("--reference", required=True)
        parser.add_argument("--provider-request-id")
        parser.add_argument(
            "--provider-stop-reason",
            choices=("end_turn", "max_tokens", "stop_sequence", "refusal"),
        )
        parser.add_argument("--input-tokens", type=int)
        parser.add_argument("--output-tokens", type=int)
        parser.add_argument("--cache-read-tokens", type=int, default=0)
        parser.add_argument("--cache-creation-tokens", type=int, default=0)
        parser.add_argument(
            "--confirm-ambiguous-provider-outcome",
            action="store_true",
            help="Required acknowledgement that provider billing evidence was reviewed.",
        )

    def handle(self, *args, **options) -> None:
        if not options["confirm_ambiguous_provider_outcome"]:
            raise CommandError("Explicit ambiguous-provider-outcome confirmation is required.")
        request_id = options["request_id"]
        if request_id <= 0:
            raise CommandError("request-id must be a positive integer.")

        schema_name = options["schema"]
        public = get_public_schema_name()
        Tenant = get_tenant_model()
        with schema_context(public):
            known = Tenant.objects.filter(schema_name=schema_name).exclude(schema_name=public).exists()
        if not known:
            raise CommandError("The selected tenant schema does not exist.")

        from apps.ai.models import AIRequest
        from apps.ai.services import Usage, reconcile_ambiguous_provider_attempt
        from core.exceptions import ConflictException

        usage = None
        if options["outcome"] == "charged":
            required = ("input_tokens", "output_tokens")
            if any(options[name] is None for name in required):
                raise CommandError("Charged reconciliation requires every provider usage field.")
            values = [
                options["input_tokens"],
                options["output_tokens"],
                options["cache_read_tokens"],
                options["cache_creation_tokens"],
            ]
            if any(value < 0 for value in values):
                raise CommandError("Provider usage values cannot be negative.")
            try:
                usage = Usage(
                    input_tokens=values[0],
                    output_tokens=values[1],
                    cache_read_tokens=values[2],
                    cache_creation_tokens=values[3],
                )
            except ValueError as exc:
                raise CommandError("Provider usage is outside the accounting bounds.") from exc

        try:
            with schema_context(schema_name):
                request = reconcile_ambiguous_provider_attempt(
                    ai_request_id=request_id,
                    outcome=options["outcome"],
                    reference=options["reference"],
                    usage=usage,
                    provider_request_id=options.get("provider_request_id") or "",
                    provider_stop_reason=options.get("provider_stop_reason") or "",
                )
        except AIRequest.DoesNotExist as exc:
            raise CommandError("The selected AI request does not exist.") from exc
        except ConflictException as exc:
            raise CommandError(f"AI provider reconciliation conflict: {exc.code}") from exc
        except ValueError as exc:
            raise CommandError("AI provider reconciliation input is invalid.") from exc

        self.stdout.write(
            json.dumps(
                {
                    "request_id": request.pk,
                    "status": request.status,
                    "outcome": request.provider_reconciliation_status,
                    "reference": request.provider_reconciliation_reference,
                    "total_tokens": (
                        request.input_tokens
                        + request.output_tokens
                        + request.cache_read_tokens
                        + request.cache_creation_tokens
                    ),
                    "cost_microusd": request.cost_microusd,
                },
                sort_keys=True,
            )
        )
