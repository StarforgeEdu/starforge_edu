"""Report unresolved role-principal ownership across workflow domains."""

from __future__ import annotations

import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from core.role_principals import PRINCIPAL_KINDS, STAFF_PRINCIPAL_KINDS


class Command(BaseCommand):
    help = (
        "Report exact-principal attribution coverage for forms, meetings, and tasks. "
        "This command is read-only and suitable for a release gate."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--schema",
            action="append",
            dest="schema_names",
            help="Limit processing to this tenant schema (repeatable).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Form audience rows examined per batch (default: 500).",
        )
        parser.add_argument(
            "--fail-on-unresolved",
            action="store_true",
            help="Exit nonzero when any legacy workflow row still needs review.",
        )

    def handle(self, *args, **options) -> None:
        batch_size = int(options["batch_size"])
        if batch_size < 1 or batch_size > 10_000:
            raise CommandError("--batch-size must be between 1 and 10000")

        reports = []
        for schema_name in self._schema_names(options.get("schema_names")):
            with schema_context(schema_name):
                report = _schema_report(schema_name, batch_size=batch_size)
            reports.append(report)
            self.stdout.write(json.dumps(report, sort_keys=True))

        totals: Counter[str] = Counter()
        for report in reports:
            totals.update({key: value for key, value in report.items() if key != "schema"})
        unresolved = sum(
            value
            for key, value in totals.items()
            if key.endswith("_unresolved") or key.endswith("_quarantined")
        )
        summary = {
            "schemas": len(reports),
            "unresolved_total": unresolved,
            **dict(sorted(totals.items())),
        }
        self.stdout.write(json.dumps(summary, sort_keys=True))
        if options["fail_on_unresolved"] and unresolved:
            raise CommandError(f"{unresolved} workflow attribution rows require review")

    @staticmethod
    def _schema_names(requested: list[str] | None) -> list[str]:
        public_schema = get_public_schema_name()
        Tenant = get_tenant_model()
        with schema_context(public_schema):
            known = set(Tenant.objects.values_list("schema_name", flat=True))
        known.discard(public_schema)
        if requested:
            unknown = set(requested) - known
            if unknown:
                raise CommandError(f"Unknown tenant schema(s): {', '.join(sorted(unknown))}")
            return list(dict.fromkeys(requested))
        return sorted(known)


def _schema_report(schema_name: str, *, batch_size: int) -> dict[str, int | str]:
    from apps.forms.models import Form, FormResponse
    from apps.meetings.models import MeetingAttendee, StaffMeeting
    from apps.tasks.models import Task

    counts: Counter[str] = Counter(
        {
            "form_audience_resolved": 0,
            "form_audience_unresolved": 0,
        }
    )
    last_pk = 0
    while True:
        forms = list(
            Form.objects.filter(pk__gt=last_pk)
            .exclude(audience_user_ids=[])
            .only("pk", "audience_user_ids", "audience_principals")
            .order_by("pk")[:batch_size]
        )
        if not forms:
            break
        last_pk = forms[-1].pk
        user_ids = {
            value
            for form in forms
            for value in form.audience_user_ids
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        } | {
            item.get("user_id")
            for form in forms
            for item in form.audience_principals
            if isinstance(item, dict)
            and isinstance(item.get("user_id"), int)
            and not isinstance(item.get("user_id"), bool)
            and item["user_id"] > 0
        }
        live_principals = _live_principal_owners(user_ids, allowed_kinds=PRINCIPAL_KINDS)
        for form in forms:
            expected = {
                value
                for value in form.audience_user_ids
                if isinstance(value, int) and not isinstance(value, bool) and value > 0
            }
            valid_items = [
                item
                for item in form.audience_principals
                if _valid_principal_target(item, allowed_kinds=PRINCIPAL_KINDS)
                and (item["kind"], item["id"], item["user_id"]) in live_principals
            ]
            captured = {item["user_id"] for item in valid_items}
            key = (
                "form_audience_resolved"
                if expected
                and expected == captured
                and len(valid_items) == len(form.audience_principals)
                and len(valid_items) == len(captured)
                else "form_audience_unresolved"
            )
            counts[key] += 1

    _count_exact_rows(
        Form,
        user_field="created_by_id",
        kind_field="created_by_principal_kind",
        principal_id_field="created_by_principal_id",
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        resolved_key="form_creator_resolved",
        unresolved_key="form_creator_quarantined",
        batch_size=batch_size,
        counts=counts,
        status_field="created_by_attribution_status",
        resolved_statuses=frozenset({"captured", "resolved"}),
        include_historical_snapshots=True,
    )
    _count_exact_rows(
        FormResponse,
        user_field="respondent_id",
        kind_field="respondent_principal_kind",
        principal_id_field="respondent_principal_id",
        allowed_kinds=PRINCIPAL_KINDS,
        resolved_key="form_response_resolved",
        unresolved_key="form_response_unresolved",
        batch_size=batch_size,
        counts=counts,
        status_field="respondent_attribution_status",
        resolved_statuses=frozenset({"captured", "resolved"}),
        include_historical_snapshots=True,
        ignored_statuses=frozenset({"anonymous"}),
    )
    counts["form_response_anonymous"] = FormResponse.objects.filter(
        respondent_attribution_status="anonymous"
    ).count()
    _count_exact_rows(
        MeetingAttendee,
        user_field="user_id",
        kind_field="principal_kind",
        principal_id_field="principal_id",
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        resolved_key="meeting_attendee_resolved",
        unresolved_key="meeting_attendee_unresolved",
        batch_size=batch_size,
        counts=counts,
    )
    _count_exact_rows(
        Task,
        user_field="assignee_id",
        kind_field="assignee_principal_kind",
        principal_id_field="assignee_principal_id",
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        resolved_key="task_assignee_resolved",
        unresolved_key="task_assignee_quarantined",
        batch_size=batch_size,
        counts=counts,
        status_field="assignee_attribution_status",
        resolved_statuses=frozenset({"captured"}),
    )
    _count_exact_rows(
        Task,
        user_field="created_by_id",
        kind_field="created_by_principal_kind",
        principal_id_field="created_by_principal_id",
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        resolved_key="task_creator_resolved",
        unresolved_key="task_creator_quarantined",
        batch_size=batch_size,
        counts=counts,
        status_field="created_by_attribution_status",
        resolved_statuses=frozenset({"captured", "resolved"}),
        include_historical_snapshots=True,
    )
    _count_exact_rows(
        StaffMeeting,
        user_field="created_by_id",
        kind_field="created_by_principal_kind",
        principal_id_field="created_by_principal_id",
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        resolved_key="meeting_creator_resolved",
        unresolved_key="meeting_creator_unresolved",
        batch_size=batch_size,
        counts=counts,
        status_field="created_by_attribution_status",
        resolved_statuses=frozenset({"captured", "resolved"}),
        include_historical_snapshots=True,
    )
    _count_exact_rows(
        StaffMeeting,
        user_field="cancelled_by_id",
        kind_field="cancelled_by_principal_kind",
        principal_id_field="cancelled_by_principal_id",
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        resolved_key="meeting_canceller_resolved",
        unresolved_key="meeting_canceller_unresolved",
        batch_size=batch_size,
        counts=counts,
        status_field="cancelled_by_attribution_status",
        resolved_statuses=frozenset({"captured", "resolved"}),
        include_historical_snapshots=True,
        ignored_statuses=frozenset({"not_applicable"}),
    )
    return {"schema": schema_name, **dict(sorted(counts.items()))}


def _live_principal_owners(user_ids, *, allowed_kinds) -> set[tuple[str, int, int]]:
    """Exact active (kind, principal id, bridge user id) tuples in fixed queries."""

    from django.apps import apps as django_apps

    from core.role_principals import PRINCIPAL_MODELS

    user_ids = set(user_ids)
    if not user_ids:
        return set()
    rows: set[tuple[str, int, int]] = set()
    for kind in sorted(allowed_kinds):
        model = django_apps.get_model(PRINCIPAL_MODELS[kind])
        rows.update(
            (kind, int(principal_id), int(user_id))
            for principal_id, user_id in model.objects.filter(
                user_id__in=user_ids,
                user__is_active=True,
                is_active=True,
            ).values_list("pk", "user_id")
        )
    return rows


def _count_exact_rows(
    model,
    *,
    user_field: str,
    kind_field: str,
    principal_id_field: str,
    allowed_kinds,
    resolved_key: str,
    unresolved_key: str,
    batch_size: int,
    counts: Counter[str],
    status_field: str | None = None,
    resolved_statuses: frozenset[str] | None = None,
    include_historical_snapshots: bool = False,
    ignored_statuses: frozenset[str] = frozenset(),
) -> None:
    """Count live ownership and immutable historical snapshots in PK batches."""

    last_pk = 0
    while True:
        queryset = model.objects.filter(pk__gt=last_pk)
        if not include_historical_snapshots:
            queryset = queryset.exclude(**{user_field: None})
        if status_field is not None and ignored_statuses:
            queryset = queryset.exclude(**{f"{status_field}__in": ignored_statuses})
        rows = list(
            queryset.values(
                "pk", user_field, kind_field, principal_id_field, *([status_field] if status_field else [])
            ).order_by("pk")[:batch_size]
        )
        if not rows:
            return
        last_pk = rows[-1]["pk"]
        owners = _live_principal_owners(
            {row[user_field] for row in rows if row[user_field] is not None},
            allowed_kinds=allowed_kinds,
        )
        for row in rows:
            snapshot = (row[kind_field], row[principal_id_field], row[user_field])
            status_ok = status_field is None or (
                resolved_statuses is not None and row[status_field] in resolved_statuses
            )
            historical_ok = bool(
                include_historical_snapshots
                and row[user_field] is None
                and row[kind_field] in allowed_kinds
                and isinstance(row[principal_id_field], int)
                and row[principal_id_field] > 0
            )
            counts[
                resolved_key if status_ok and (snapshot in owners or historical_ok) else unresolved_key
            ] += 1


def _valid_principal_target(item, *, allowed_kinds) -> bool:
    return bool(
        isinstance(item, dict)
        and item.get("kind") in allowed_kinds
        and isinstance(item.get("id"), int)
        and not isinstance(item.get("id"), bool)
        and item["id"] > 0
        and isinstance(item.get("user_id"), int)
        and not isinstance(item.get("user_id"), bool)
        and item["user_id"] > 0
    )
