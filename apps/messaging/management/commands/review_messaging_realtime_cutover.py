"""Review principal attribution and cursor integrity before realtime cutover."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Exists, F, Max, Min, OuterRef, Q
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context


def _tenant_schemas(requested: list[str]) -> list[str]:
    Tenant = get_tenant_model()
    with schema_context(get_public_schema_name()):
        available = set(
            Tenant.objects.filter(is_active=True, archived_at__isnull=True).values_list(
                "schema_name", flat=True
            )
        )
    if not requested:
        return sorted(available)
    normalized = list(dict.fromkeys(requested))
    unknown = sorted(set(normalized) - available)
    if unknown:
        raise CommandError(f"Unknown or inactive tenant schema(s): {', '.join(unknown)}")
    return normalized


def _inspect(schema: str) -> dict[str, int | str]:
    from apps.messaging.models import Message, Thread, ThreadParticipant, ThreadRealtimeEvent

    with schema_context(schema):
        participant_counts = {
            str(row["attribution_status"]): int(row["count"])
            for row in ThreadParticipant.objects.values("attribution_status").annotate(count=Count("id"))
        }
        sender_counts = {
            str(row["sender_attribution_status"]): int(row["count"])
            for row in Message.objects.values("sender_attribution_status").annotate(count=Count("id"))
        }
        matching_message_event = ThreadRealtimeEvent.objects.filter(
            thread_id=OuterRef("thread_id"),
            message_id=OuterRef("pk"),
            kind="message.created",
        )
        captured_without_event = (
            Message.objects.filter(sender_attribution_status="captured")
            .filter(~Exists(matching_message_event))
            .count()
        )
        invalid_read_cursor = (
            ThreadParticipant.objects.filter(last_read_message_id__isnull=False)
            .exclude(last_read_message__thread_id=F("thread_id"))
            .count()
        )
        event_rollups = ThreadRealtimeEvent.objects.values("thread_id").annotate(
            minimum=Min("sequence"),
            maximum=Max("sequence"),
            count=Count("id"),
        )
        event_gap_threads = event_rollups.exclude(
            minimum=1,
            maximum=F("count"),
        ).count()
        sequence_mismatch_threads = (
            Thread.objects.annotate(
                actual_sequence=Max("realtime_events__sequence"),
            )
            .filter(
                Q(actual_sequence__isnull=True, realtime_sequence__gt=0)
                | (Q(actual_sequence__isnull=False) & ~Q(realtime_sequence=F("actual_sequence")))
            )
            .count()
        )
        return {
            "schema": schema,
            "participants_captured": participant_counts.get("captured", 0),
            "participants_resolved": participant_counts.get("resolved", 0),
            "participants_unresolved": participant_counts.get("unresolved", 0),
            "participants_conflicting": participant_counts.get("conflicting", 0),
            "participants_quarantined": participant_counts.get("quarantined", 0),
            "senders_captured": sender_counts.get("captured", 0),
            "senders_resolved": sender_counts.get("resolved", 0),
            "senders_unresolved": sender_counts.get("unresolved", 0),
            "senders_conflicting": sender_counts.get("conflicting", 0),
            "senders_quarantined": sender_counts.get("quarantined", 0),
            "captured_messages_without_event": captured_without_event,
            "invalid_read_cursors": invalid_read_cursor,
            "event_gap_threads": event_gap_threads,
            "sequence_mismatch_threads": sequence_mismatch_threads,
        }


class Command(BaseCommand):
    help = "Report privacy-safe messaging realtime migration/cursor integrity counts."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--schema", action="append", default=[])
        parser.add_argument("--fail-on-blocked", action="store_true")
        parser.add_argument("--fail-on-unresolved", action="store_true")

    def handle(self, *args, **options):
        reports = [_inspect(schema) for schema in _tenant_schemas(options["schema"])]
        blocked_keys = (
            "captured_messages_without_event",
            "invalid_read_cursors",
            "event_gap_threads",
            "sequence_mismatch_threads",
        )
        unresolved_keys = (
            "participants_unresolved",
            "participants_conflicting",
            "participants_quarantined",
            "senders_unresolved",
            "senders_conflicting",
            "senders_quarantined",
        )
        payload = {
            "reports": reports,
            "blocked": sum(int(row[key]) for row in reports for key in blocked_keys),
            "unresolved": sum(int(row[key]) for row in reports for key in unresolved_keys),
        }
        self.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        if options["fail_on_blocked"] and payload["blocked"]:
            raise CommandError("Messaging realtime integrity blockers require review.")
        if options["fail_on_unresolved"] and payload["unresolved"]:
            raise CommandError("Messaging principal attribution rows require review.")
