"""Review and optionally backfill immutable scope on legacy audit rows.

The command is report-only unless ``--apply`` is supplied.  It relies solely on
the row's already-frozen resource type/id and before/after snapshots.  It never
joins a resource's current branch, so a student transfer or later organization
edit cannot rewrite history by inference.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.audit.models import AuditLog
from apps.audit.scopes import ORGANIZATION, SCOPED, resolve_audit_scope


class Command(BaseCommand):
    help = (
        "Review legacy AuditLog scope from immutable stored evidence. Defaults "
        "to report-only; pass --apply to write resolved scope snapshots."
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
            help="Rows reviewed per bounded batch (default: 1000).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist only safely resolved scoped/organization classifications.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size = options["batch_size"]
        if batch_size < 1 or batch_size > 10_000:
            raise CommandError("--batch-size must be between 1 and 10000")

        schemas = _schema_names(options.get("schema_names"))
        apply_changes = bool(options["apply"])
        totals: Counter[str] = Counter()
        for schema_name in schemas:
            with schema_context(schema_name):
                report = _review_schema(
                    schema_name=schema_name,
                    batch_size=batch_size,
                    apply_changes=apply_changes,
                )
            totals.update({key: value for key, value in report.items() if isinstance(value, int)})
            self.stdout.write(json.dumps(report, sort_keys=True))

        summary = {
            "mode": "apply" if apply_changes else "review",
            "schemas": len(schemas),
            **dict(sorted(totals.items())),
        }
        self.stdout.write(self.style.SUCCESS(json.dumps(summary, sort_keys=True)))


def _schema_names(requested: list[str] | None) -> list[str]:
    public_schema = get_public_schema_name()
    tenant_model = get_tenant_model()
    with schema_context(public_schema):
        known = {
            public_schema,
            *tenant_model.objects.values_list("schema_name", flat=True),
        }
    if not requested:
        return sorted(known)
    unknown = set(requested) - known
    if unknown:
        raise CommandError(f"Unknown schema(s): {', '.join(sorted(unknown))}")
    return list(dict.fromkeys(requested))


def _review_schema(
    *,
    schema_name: str,
    batch_size: int,
    apply_changes: bool,
) -> dict[str, str | int]:
    counts: Counter[str] = Counter()
    counts["already_classified"] = AuditLog.objects.exclude(
        scope_status=AuditLog.ScopeStatus.UNRESOLVED
    ).count()
    upper_pk = AuditLog.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    last_pk = 0
    while True:
        rows = list(
            AuditLog.objects.filter(
                scope_status=AuditLog.ScopeStatus.UNRESOLVED,
                pk__gt=last_pk,
                pk__lte=upper_pk,
            )
            .order_by("pk")
            .values("pk", "resource_type", "resource_id", "before", "after")[:batch_size]
        )
        if not rows:
            break
        last_pk = rows[-1]["pk"]
        updates: list[tuple[str, int | None, int | None, int]] = []
        for row in rows:
            resolution = resolve_audit_scope(
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                before=row["before"],
                after=row["after"],
            )
            counts["reviewed"] += 1
            if resolution.reason in {"ambiguous_snapshot", "conflicting_snapshots"}:
                counts["quarantined"] += 1
                continue
            if resolution.scope.status == SCOPED:
                counts["resolved_scoped"] += 1
            elif resolution.scope.status == ORGANIZATION:
                counts["resolved_organization"] += 1
            else:
                counts["unresolved"] += 1
                continue
            updates.append(
                (
                    resolution.scope.status,
                    resolution.scope.branch_id,
                    resolution.scope.department_id,
                    row["pk"],
                )
            )

        if apply_changes and updates:
            counts["updated"] += _apply_batch(updates)

    return {
        "schema": schema_name,
        "mode": "apply" if apply_changes else "review",
        "already_classified": counts["already_classified"],
        "reviewed": counts["reviewed"],
        "resolved_scoped": counts["resolved_scoped"],
        "resolved_organization": counts["resolved_organization"],
        "unresolved": counts["unresolved"],
        "quarantined": counts["quarantined"],
        "updated": counts["updated"],
    }


def _apply_batch(updates: list[tuple[str, int | None, int | None, int]]) -> int:
    updated = 0
    # Migration/retention are the only reviewed maintenance paths allowed past
    # the append-only trigger. SET LOCAL cannot escape this transaction.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.audit_maintenance = 'scope-backfill'")
        for status, branch_id, department_id, row_id in updates:
            cursor.execute(
                """
                UPDATE audit_auditlog
                   SET scope_status = %s,
                       scope_branch_id = %s,
                       scope_department_id = %s
                 WHERE id = %s
                   AND scope_status = 'unresolved'
                """,
                [status, branch_id, department_id, row_id],
            )
            updated += cursor.rowcount
    return updated
