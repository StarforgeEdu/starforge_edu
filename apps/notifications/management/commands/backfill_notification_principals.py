"""Review and optionally classify legacy notification recipient ownership."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.notifications.models import (
    DELIVERABLE_ATTRIBUTION_STATUSES,
    Notification,
    NotificationPreference,
    RecipientAttributionStatus,
)
from apps.notifications.principals import resolve_recipient_principals


class _RowReportSpool:
    """Disk-backed row sink used to assemble one valid JSON report."""

    def __init__(self, *, enabled: bool) -> None:
        self._handle = (
            tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115 - owned by close()
            if enabled
            else None
        )

    def append(self, row: dict[str, Any]) -> None:
        if self._handle is not None:
            self._handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")

    def write_report(
        self,
        path: Path,
        *,
        summary: dict[str, Any],
        schemas: list[dict[str, Any]],
    ) -> None:
        if self._handle is None:
            return
        temporary_name = ""
        descriptor = -1
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                descriptor = -1
                destination.write('{"summary":')
                json.dump(summary, destination, sort_keys=True, separators=(",", ":"))
                destination.write(',"schemas":')
                json.dump(schemas, destination, sort_keys=True, separators=(",", ":"))
                destination.write(',"rows":[')
                self._handle.seek(0)
                first = True
                for line in self._handle:
                    if not first:
                        destination.write(",")
                    destination.write(line.rstrip("\n"))
                    first = False
                destination.write("]}\n")
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary_name, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()


class _DiskIdentityCounts:
    """Bounded-memory global uniqueness counts, independent of review batches."""

    def __init__(self) -> None:
        descriptor, self._path = tempfile.mkstemp(prefix="notification-principals-", suffix=".sqlite3")
        os.close(descriptor)
        self._database = sqlite3.connect(self._path)
        self._database.execute(
            "CREATE TABLE identity_counts (scope TEXT NOT NULL, identity TEXT NOT NULL, "
            "occurrences INTEGER NOT NULL, PRIMARY KEY (scope, identity))"
        )

    def add_many(self, scope: str, keys: list[tuple]) -> None:
        self._database.executemany(
            "INSERT INTO identity_counts(scope, identity, occurrences) VALUES (?, ?, 1) "
            "ON CONFLICT(scope, identity) DO UPDATE SET occurrences = occurrences + 1",
            ((scope, json.dumps(key, separators=(",", ":"))) for key in keys),
        )
        self._database.commit()

    def count(self, scope: str, key: tuple) -> int:
        row = self._database.execute(
            "SELECT occurrences FROM identity_counts WHERE scope = ? AND identity = ?",
            (scope, json.dumps(key, separators=(",", ":"))),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def close(self) -> None:
        self._database.close()
        with suppress(FileNotFoundError):
            os.unlink(self._path)


class Command(BaseCommand):
    help = (
        "Review immutable role-principal ownership for legacy notifications and "
        "preferences. Defaults to report-only; --apply writes only proven rows "
        "while retaining ambiguous evidence in a non-deliverable review state."
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
            help="Rows reviewed per bounded batch (default: 500).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist reviewed resolutions. Without this flag no rows change.",
        )
        parser.add_argument(
            "--report",
            help="Optional path for a complete JSON row-level evidence report.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size = int(options["batch_size"])
        if batch_size < 1 or batch_size > 10_000:
            raise CommandError("--batch-size must be between 1 and 10000")

        row_report = _RowReportSpool(enabled=bool(options.get("report")))
        identity_counts = _DiskIdentityCounts()
        schemas: list[dict[str, Any]] = []
        try:
            for schema_name in _schema_names(options.get("schema_names")):
                with schema_context(schema_name):
                    schema_report = _review_schema(
                        schema_name=schema_name,
                        batch_size=batch_size,
                        apply_changes=bool(options["apply"]),
                        row_report=row_report,
                        identity_counts=identity_counts,
                    )
                schemas.append(schema_report)
                self.stdout.write(json.dumps(schema_report, sort_keys=True))

            totals: Counter[str] = Counter()
            for report in schemas:
                totals.update(
                    {
                        key: value
                        for key, value in report.items()
                        if key != "schema" and isinstance(value, int)
                    }
                )
            summary: dict[str, Any] = {
                "mode": "apply" if options["apply"] else "review",
                "schemas": len(schemas),
                **dict(sorted(totals.items())),
            }
            if options.get("report"):
                path = Path(options["report"]).expanduser().absolute()
                if not path.parent.is_dir():
                    raise CommandError(f"Report directory does not exist: {path.parent}")
                row_report.write_report(path, summary=summary, schemas=schemas)
            self.stdout.write(self.style.SUCCESS(json.dumps(summary, sort_keys=True)))
        finally:
            identity_counts.close()
            row_report.close()


def _write_private_report(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write row identifiers with owner-only permissions.

    Replacing the destination rather than following it also prevents a stale
    symlink at the operator-supplied path from redirecting evidence into another
    file.
    """

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    temporary_name = ""
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)


def _schema_names(requested: list[str] | str | None) -> list[str]:
    # ``argparse`` supplies a list for the repeatable CLI option, while
    # ``call_command(..., schema="tenant_a")`` supplies the scalar value passed
    # by the caller.  Normalize both forms before set arithmetic; treating a
    # scalar string as an iterable would otherwise validate individual
    # characters as schema names.
    if isinstance(requested, str):
        requested = [requested]

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


def _review_schema(
    *,
    schema_name: str,
    batch_size: int,
    apply_changes: bool,
    row_report: _RowReportSpool,
    identity_counts: _DiskIdentityCounts,
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    _review_model(
        Notification,
        model_label="notification",
        batch_size=batch_size,
        apply_changes=apply_changes,
        counts=counts,
        row_report=row_report,
        identity_counts=identity_counts,
        identity_scope=f"{schema_name}:notification",
    )
    _review_model(
        NotificationPreference,
        model_label="preference",
        batch_size=batch_size,
        apply_changes=apply_changes,
        counts=counts,
        row_report=row_report,
        identity_counts=identity_counts,
        identity_scope=f"{schema_name}:preference",
    )
    return {
        "schema": schema_name,
        **dict(sorted(counts.items())),
    }


def _review_model(
    model,
    *,
    model_label: str,
    batch_size: int,
    apply_changes: bool,
    counts: Counter[str],
    row_report: _RowReportSpool,
    identity_counts: _DiskIdentityCounts,
    identity_scope: str,
) -> None:
    counts[f"{model_label}_already_attributed"] = model.objects.filter(
        attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES
    ).count()
    upper_pk = model.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    _stage_identity_counts(
        model,
        upper_pk=upper_pk,
        batch_size=batch_size,
        identity_counts=identity_counts,
        identity_scope=identity_scope,
    )
    last_pk = 0
    while True:
        queryset = (
            model.objects.filter(
                pk__gt=last_pk,
                pk__lte=upper_pk,
            )
            .exclude(attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES)
            .order_by("pk")
        )
        if model is Notification:
            queryset = queryset.only("pk", "user_id", "dedupe_key", "attribution_status")
        else:
            queryset = queryset.only(
                "pk",
                "user_id",
                "event_type",
                "channel",
                "attribution_status",
            )
        rows = list(queryset[:batch_size])
        if not rows:
            break
        last_pk = rows[-1].pk
        resolutions = resolve_recipient_principals(row.user_id for row in rows)
        candidate_keys = {
            row.pk: _candidate_identity_key(model, row, resolutions[row.user_id]) for row in rows
        }
        candidate_keys_in_batch = {key for key in candidate_keys.values() if key is not None}
        existing_keys = _existing_identity_keys(model, candidate_keys_in_batch)
        updates: list[tuple[int, str | None, int | None, str]] = []
        for row in rows:
            resolution = resolutions[row.user_id]
            target_status = resolution.status
            kind = resolution.kind
            principal_id = resolution.principal_id
            if resolution.is_deliverable:
                target_status = RecipientAttributionStatus.RESOLVED
                candidate_key = candidate_keys[row.pk]
                if candidate_key is not None and (
                    identity_counts.count(identity_scope, candidate_key) > 1 or candidate_key in existing_keys
                ):
                    target_status = RecipientAttributionStatus.CONFLICTING
                    kind = None
                    principal_id = None
                    reason = "principal_identity_conflict"
                    counts[f"{model_label}_{target_status}"] += 1
                else:
                    reason = resolution.reason
                    counts[f"{model_label}_resolvable"] += 1
            else:
                reason = resolution.reason
                counts[f"{model_label}_{target_status}"] += 1
            counts[f"{model_label}_reviewed"] += 1
            row_report.append(
                {
                    "model": model_label,
                    "id": row.pk,
                    "user_id": row.user_id,
                    "status": target_status,
                    "principal_kind": kind,
                    "principal_id": principal_id,
                    "reason": reason,
                }
            )
            if apply_changes:
                updates.append((row.pk, kind, principal_id, target_status))
        if updates:
            counts[f"{model_label}_updated"] += _apply_batch(model, updates)


def _stage_identity_counts(
    model,
    *,
    upper_pk: int,
    batch_size: int,
    identity_counts: _DiskIdentityCounts,
    identity_scope: str,
) -> None:
    """First pass: count every candidate key without retaining rows in RAM."""

    last_pk = 0
    while True:
        queryset = (
            model.objects.filter(pk__gt=last_pk, pk__lte=upper_pk)
            .exclude(attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES)
            .order_by("pk")
        )
        if model is Notification:
            queryset = queryset.only("pk", "user_id", "dedupe_key", "attribution_status")
        else:
            queryset = queryset.only(
                "pk",
                "user_id",
                "event_type",
                "channel",
                "attribution_status",
            )
        rows = list(queryset[:batch_size])
        if not rows:
            return
        last_pk = rows[-1].pk
        resolutions = resolve_recipient_principals(row.user_id for row in rows)
        keys = [
            key
            for row in rows
            if (key := _candidate_identity_key(model, row, resolutions[row.user_id])) is not None
        ]
        identity_counts.add_many(identity_scope, keys)


def _candidate_identity_key(model, row, resolution) -> tuple | None:
    if not resolution.is_deliverable or resolution.kind is None or resolution.principal_id is None:
        return None
    if model is Notification:
        if not row.dedupe_key:
            return None
        return (resolution.kind, resolution.principal_id, row.dedupe_key)
    return (resolution.kind, resolution.principal_id, row.event_type, row.channel)


def _existing_identity_keys(model, keys: set[tuple]) -> set[tuple]:
    """Load exact uniqueness conflicts in bounded SQL chunks, never per row."""

    found: set[tuple] = set()
    key_list = list(keys)
    for start in range(0, len(key_list), 100):
        predicate = Q()
        for key in key_list[start : start + 100]:
            if model is Notification:
                kind, principal_id, dedupe_key = key
                predicate |= Q(
                    recipient_principal_kind=kind,
                    recipient_principal_id=principal_id,
                    dedupe_key=dedupe_key,
                )
            else:
                kind, principal_id, event_type, channel = key
                predicate |= Q(
                    recipient_principal_kind=kind,
                    recipient_principal_id=principal_id,
                    event_type=event_type,
                    channel=channel,
                )
        if not predicate:
            continue
        queryset = model.objects.filter(
            predicate,
            attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES,
        )
        if model is Notification:
            found.update(
                queryset.values_list(
                    "recipient_principal_kind",
                    "recipient_principal_id",
                    "dedupe_key",
                )
            )
        else:
            found.update(
                queryset.values_list(
                    "recipient_principal_kind",
                    "recipient_principal_id",
                    "event_type",
                    "channel",
                )
            )
    return found


def _apply_batch(model, updates: list[tuple[int, str | None, int | None, str]]) -> int:
    table = model._meta.db_table
    statements = {
        "notifications_notification": """
            UPDATE notifications_notification
               SET recipient_principal_kind = %s,
                   recipient_principal_id = %s,
                   attribution_status = %s
             WHERE id = %s
               AND attribution_status IN ('unresolved', 'conflicting', 'quarantined')
        """,
        "notifications_notificationpreference": """
            UPDATE notifications_notificationpreference
               SET recipient_principal_kind = %s,
                   recipient_principal_id = %s,
                   attribution_status = %s
             WHERE id = %s
               AND attribution_status IN ('unresolved', 'conflicting', 'quarantined')
        """,
    }
    statement = statements.get(table)
    if statement is None:
        raise CommandError("Unexpected notification attribution table")
    updated = 0
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.notification_maintenance = 'principal-backfill'")
        for row_id, kind, principal_id, status in updates:
            cursor.execute(statement, [kind, principal_id, status, row_id])
            updated += cursor.rowcount
    return updated
