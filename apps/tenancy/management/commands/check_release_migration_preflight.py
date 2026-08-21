"""Read-only, all-tenant preflight for release-blocking migrations.

The migration executor applies tenant schemas one at a time.  A data-dependent
preflight embedded in a later migration can therefore discover bad legacy data
only after earlier schemas have already committed.  This command mirrors the
fail-closed checks that can be evaluated against the legacy schema and runs
them for every tenant before the deployment takes its quiesced backup.

Only aggregate counts and a one-way schema reference are emitted.  Row values,
tenant schema names, encryption keys, and provider payloads never enter release
logs or evidence files.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.tenancy.management.commands.check_safeguarding_encryption_cutover import (
    REQUIRED_MIGRATIONS,
    requires_safeguarding_cutover,
)

_APPROVED_MODEL_TARGETS = frozenset(
    {
        ("academics", "ExamType"),
        ("academics", "Exam"),
        ("academics", "ExamResult"),
        ("academics", "Subject"),
        ("access", "AccountType"),
        ("access", "AccountTypePermission"),
        ("access", "RolePermissionOverride"),
        ("ai_app", "AIRequest"),
        ("audit", "AuditLog"),
        ("finance", "Invoice"),
        ("forms_app", "Form"),
        ("notifications", "Notification"),
        ("notifications", "NotificationPreference"),
        ("org", "Branch"),
        ("org", "BranchTransfer"),
        ("org", "CenterSettings"),
        ("org", "Department"),
        ("org", "Room"),
        ("parents", "Guardian"),
        ("parents", "ParentProfile"),
        ("payments", "Payment"),
        ("reports", "ReportRun"),
        ("reports", "ReportSchedule"),
        ("students", "StudentProfile"),
    }
)
_WORKLOAD_FIELD_PREDICATES = {
    "custody_notes": "{column} IS NOT NULL AND {column} <> ''",
    "emergency_contacts": "{column} IS NOT NULL AND {column} <> '[]'::jsonb",
    "notes": "{column} IS NOT NULL AND {column} <> ''",
}


@dataclass(frozen=True)
class _TableReference:
    """Keep parameter-safe metadata separate from an interpolation-safe identifier."""

    name: str
    sql: str

    def __str__(self) -> str:
        return self.sql


def _schema_ref(schema_name: str) -> str:
    """Return a stable non-reversible reference suitable for operator evidence."""

    return hashlib.sha256(schema_name.encode("utf-8")).hexdigest()[:16]


def _table(app_label: str, model_name: str) -> _TableReference:
    """Return raw and quoted forms for one model in the closed inventory."""

    target = (app_label, model_name)
    if target not in _APPROVED_MODEL_TARGETS:
        raise ValueError("Release preflight model target is not approved.")
    table_name = django_apps.get_model(app_label, model_name)._meta.db_table
    return _TableReference(name=table_name, sql=connection.ops.quote_name(table_name))


def _workload_predicate(field_name: str) -> str:
    """Return a predicate built only from a quoted, explicitly allowed column."""

    try:
        template = _WORKLOAD_FIELD_PREDICATES[field_name]
    except KeyError as exc:
        raise ValueError("Release preflight workload field is not approved.") from exc
    return template.format(column=connection.ops.quote_name(field_name))


def _scalar(cursor, sql: str, params: Iterable[object] = ()) -> int:
    cursor.execute(sql, list(params))
    row = cursor.fetchone()
    return int(row[0]) if row is not None else 0


def _relation_exists(cursor, table: _TableReference) -> bool:
    cursor.execute(
        "SELECT EXISTS ("
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = %s"
        ")",
        [table.name],
    )
    row = cursor.fetchone()
    return bool(row and row[0])


def _pending(applied: set[tuple[str, str]], migration: tuple[str, str]) -> bool:
    return migration not in applied


def _academic_checks(cursor, applied, issues, estimates) -> None:
    if not _pending(applied, ("academics", "0004_assessment_integrity")):
        return
    for app_label, model_name, evidence_name in (
        ("academics", "Subject", "academic_subject_duplicate_groups"),
        ("academics", "ExamType", "academic_exam_type_duplicate_groups"),
    ):
        table = _table(app_label, model_name)
        if not _relation_exists(cursor, table):
            continue
        count = _scalar(
            cursor,
            f"""
            SELECT count(*)
              FROM (
                    SELECT lower(name)
                      FROM {table}
                     GROUP BY lower(name)
                    HAVING count(*) > 1
                   ) duplicates
            """,  # nosec B608
        )
        issues[evidence_name] = count
        estimates[f"{evidence_name}_rows"] = count

    exam_table = _table("academics", "Exam")
    result_table = _table("academics", "ExamResult")
    if _relation_exists(cursor, exam_table):
        issues["academic_invalid_exam_numeric_rows"] = _scalar(
            cursor,
            f"""
            SELECT count(*)
              FROM {exam_table}
             WHERE max_score::text IN ('NaN', 'Infinity', '-Infinity')
                OR weight::text IN ('NaN', 'Infinity', '-Infinity')
                OR max_score <= 0
                OR weight <= 0
            """,  # nosec B608
        )
    if _relation_exists(cursor, result_table) and _relation_exists(cursor, exam_table):
        issues["academic_invalid_result_score_rows"] = _scalar(
            cursor,
            f"""
            SELECT count(*)
              FROM {result_table} result
              JOIN {exam_table} exam ON exam.id = result.exam_id
             WHERE result.score::text IN ('NaN', 'Infinity', '-Infinity')
                OR exam.max_score::text IN ('NaN', 'Infinity', '-Infinity')
                OR result.score < 0
                OR result.score > exam.max_score
            """,  # nosec B608
        )


def _access_checks(cursor, applied, issues) -> None:
    if not _pending(applied, ("access", "0005_protect_owner_authority")):
        return
    type_table = _table("access", "AccountType")
    permission_table = _table("access", "AccountTypePermission")
    override_table = _table("access", "RolePermissionOverride")
    if not all(_relation_exists(cursor, table) for table in (type_table, permission_table, override_table)):
        return
    owner_count = _scalar(
        cursor,
        f"SELECT count(*) FROM {type_table} WHERE is_system AND slug = 'director'",  # nosec B608
    )
    issues["access_system_owner_count_invalid"] = int(owner_count != 1)
    if owner_count != 1:
        return
    cursor.execute(
        f"SELECT id FROM {type_table} WHERE is_system AND slug = 'director'",  # nosec B608
    )
    owner_id = int(cursor.fetchone()[0])
    wildcard_count = _scalar(
        cursor,
        f"SELECT count(*) FROM {permission_table} WHERE account_type_id = %s AND permission = '*:*'",  # nosec B608
        [owner_id],
    )
    issues["access_owner_wildcard_missing"] = int(wildcard_count == 0)
    issues["access_reserved_overrides"] = _scalar(
        cursor,
        f"SELECT count(*) FROM {override_table} WHERE permission LIKE 'access:%%'",  # nosec B608
    )
    issues["access_reserved_non_owner_grants"] = _scalar(
        cursor,
        f"""
        SELECT count(*)
          FROM {permission_table}
         WHERE account_type_id <> %s
           AND (permission = '*:*' OR permission LIKE 'access:%%')
        """,  # nosec B608
        [owner_id],
    )


def _ai_checks(cursor, applied, issues, estimates) -> None:
    if not _pending(applied, ("ai_app", "0015_ai_request_scope_privacy")):
        return
    table = _table("ai_app", "AIRequest")
    if not _relation_exists(cursor, table):
        return
    issues["ai_negative_cost_rows"] = _scalar(
        cursor,
        f"SELECT count(*) FROM {table} WHERE cost_microusd < 0",  # nosec B608
    )
    issues["ai_inflight_requests"] = _scalar(
        cursor,
        f"SELECT count(*) FROM {table} WHERE status IN ('queued', 'running')",  # nosec B608
    )
    estimates["legacy_ai_requests_requiring_quarantine"] = _scalar(
        cursor,
        f"SELECT count(*) FROM {table}",  # nosec B608
    )
    estimates["legacy_ai_plaintext_outputs_to_protect"] = _scalar(
        cursor,
        f"SELECT count(*) FROM {table} WHERE output_text IS NOT NULL AND output_text <> ''",  # nosec B608
    )


def _form_checks(cursor, applied, issues) -> None:
    if not _pending(applied, ("forms_app", "0003_role_principal_attribution")):
        return
    table = _table("forms_app", "Form")
    if _relation_exists(cursor, table):
        issues["forms_invalid_response_windows"] = _scalar(
            cursor,
            f"""
            SELECT count(*) FROM {table}
             WHERE opens_at IS NOT NULL
               AND closes_at IS NOT NULL
               AND closes_at <= opens_at
            """,  # nosec B608
        )


def _payment_checks(cursor, applied, issues, estimates) -> None:
    payment_table = _table("payments", "Payment")
    if not _relation_exists(cursor, payment_table):
        return
    if _pending(applied, ("payments", "0008_external_provider_transaction_integrity")):
        issues["payment_duplicate_provider_transaction_groups"] = _scalar(
            cursor,
            f"""
            SELECT count(*)
              FROM (
                    SELECT provider, provider_txn_id
                      FROM {payment_table}
                     WHERE provider IN ('click', 'payme', 'uzum')
                       AND provider_txn_id <> ''
                     GROUP BY provider, provider_txn_id
                    HAVING count(*) > 1
                   ) duplicates
            """,  # nosec B608
        )
    if _pending(applied, ("payments", "0005_payment_historical_scope")):
        estimates["legacy_payments_requiring_scope_review"] = _scalar(
            cursor,
            f"SELECT count(*) FROM {payment_table}",  # nosec B608
        )


def _money_unit_checks(cursor, applied, issues) -> None:
    """Block constraints that would otherwise fail tenant-by-tenant.

    Invoice.total_uzs and Payment.amount_uzs have always encoded UZS values.
    A different value in the adjacent currency label is ambiguous legacy data,
    not evidence of a conversion, so release automation must never repair it by
    guessing.
    """

    targets = (
        (
            ("finance", "0010_invoice_currency_uzs"),
            "finance",
            "Invoice",
            "finance_invoice_non_uzs_currency_rows",
        ),
        (
            ("payments", "0009_payment_currency_uzs"),
            "payments",
            "Payment",
            "payments_non_uzs_currency_rows",
        ),
    )
    for migration, app_label, model_name, evidence_name in targets:
        if not _pending(applied, migration):
            continue
        table = _table(app_label, model_name)
        if _relation_exists(cursor, table):
            issues[evidence_name] = _scalar(
                cursor,
                f"SELECT count(*) FROM {table} WHERE currency IS DISTINCT FROM %s",  # nosec B608
                ("UZS",),
            )


def _organization_checks(cursor, applied, issues, estimates) -> None:
    if _pending(applied, ("org", "0020_org_scope_and_history_integrity")):
        branch_table = _table("org", "Branch")
        department_table = _table("org", "Department")
        room_table = _table("org", "Room")
        transfer_table = _table("org", "BranchTransfer")
        if _relation_exists(cursor, branch_table):
            cursor.execute(f"SELECT timezone FROM {branch_table}")  # nosec B608
            invalid_timezones = 0
            for (timezone_name,) in cursor.fetchall():
                try:
                    ZoneInfo(timezone_name)
                except (TypeError, ValueError, ZoneInfoNotFoundError):
                    invalid_timezones += 1
            issues["organization_invalid_timezones"] = invalid_timezones
        if _relation_exists(cursor, department_table):
            issues["organization_negative_department_budgets"] = _scalar(
                cursor,
                f"SELECT count(*) FROM {department_table} WHERE budget < 0",  # nosec B608
            )
            issues["organization_oversized_department_descriptions"] = _scalar(
                cursor,
                f"SELECT count(*) FROM {department_table} WHERE length(description) > 4000",  # nosec B608
            )
        if _relation_exists(cursor, room_table):
            cursor.execute(f"SELECT equipment FROM {room_table}")  # nosec B608
            invalid_equipment = 0
            for (equipment,) in cursor.fetchall():
                valid = isinstance(equipment, list) and len(equipment) <= 64
                normalized: list[str] = []
                if valid:
                    for item in equipment:
                        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 100:
                            valid = False
                            break
                        normalized.append(item.strip())
                    valid = valid and len(normalized) == len(set(normalized))
                invalid_equipment += int(not valid)
            issues["organization_invalid_room_equipment"] = invalid_equipment
            issues["organization_oversized_room_notes"] = _scalar(
                cursor,
                f"SELECT count(*) FROM {room_table} WHERE length(notes) > 4000",  # nosec B608
            )
        if _relation_exists(cursor, transfer_table):
            estimates["legacy_branch_transfers_requiring_attribution"] = _scalar(
                cursor,
                f"SELECT count(*) FROM {transfer_table}",  # nosec B608
            )

    if not _pending(applied, ("org", "0021_durable_center_settings")):
        return
    table = _table("org", "CenterSettings")
    if not _relation_exists(cursor, table):
        return
    cursor.execute(
        f"""
        SELECT id, academic_warning_max, honor_roll_min,
               sibling_discount_percent, fx_source, fx_rate_usd_manual,
               currency_primary, currency_secondary
          FROM {table}
         ORDER BY id
        """  # nosec B608
    )
    rows = cursor.fetchall()
    issues["center_settings_singleton_invalid"] = int(
        len(rows) > 1 or (len(rows) == 1 and int(rows[0][0]) != 1)
    )
    if not rows:
        return
    (
        _row_id,
        warning_max,
        honor_min,
        sibling_discount,
        fx_source,
        manual_rate,
        primary_currency,
        secondary_currency,
    ) = rows[0]
    invalid_policy_fields = 0
    invalid_policy_fields += int(not Decimal("0") <= warning_max <= honor_min <= Decimal("100"))
    invalid_policy_fields += int(not Decimal("0") <= sibling_discount <= Decimal("100"))
    invalid_policy_fields += int(fx_source not in {"cbu", "manual"})
    invalid_policy_fields += int(manual_rate is not None and manual_rate <= 0)
    invalid_policy_fields += int(fx_source == "manual" and manual_rate is None)
    for currency in (primary_currency, secondary_currency):
        invalid_policy_fields += int(
            not isinstance(currency, str)
            or len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        )
    invalid_policy_fields += int(primary_currency == secondary_currency)
    issues["center_settings_invalid_policy_fields"] = invalid_policy_fields


def _workload_estimates(cursor, applied, estimates) -> None:
    checks = (
        (
            ("parents", "0009_encrypt_safeguarding_text"),
            "parents",
            "ParentProfile",
            "notes",
            "parent_notes_to_encrypt",
        ),
        (
            ("parents", "0009_encrypt_safeguarding_text"),
            "parents",
            "Guardian",
            "custody_notes",
            "guardian_notes_to_encrypt",
        ),
        (
            ("students", "0010_encrypt_emergency_contacts"),
            "students",
            "StudentProfile",
            "emergency_contacts",
            "student_contacts_to_encrypt",
        ),
    )
    for migration, app_label, model_name, field_name, evidence_name in checks:
        if not _pending(applied, migration):
            continue
        table = _table(app_label, model_name)
        if not _relation_exists(cursor, table):
            continue
        predicate = _workload_predicate(field_name)
        estimates[evidence_name] = _scalar(
            cursor,
            f"SELECT count(*) FROM {table} WHERE {predicate}",  # nosec B608
        )

    row_estimates = (
        (("audit", "0005_audit_scope_snapshot"), "audit", "AuditLog", "audit_rows_requiring_scope_review"),
        (
            ("audit", "0006_actor_principal_snapshot"),
            "audit",
            "AuditLog",
            "legacy_audit_actors_marked_unresolved",
        ),
        (
            ("finance", "0009_invoice_historical_scope"),
            "finance",
            "Invoice",
            "legacy_invoices_requiring_scope_review",
        ),
        (
            ("parents", "0008_parent_creation_scope"),
            "parents",
            "ParentProfile",
            "legacy_parent_profiles_requiring_scope_review",
        ),
        (
            ("notifications", "0012_recipient_principal_attribution"),
            "notifications",
            "Notification",
            "legacy_notifications_requiring_attribution",
        ),
        (
            ("notifications", "0012_recipient_principal_attribution"),
            "notifications",
            "NotificationPreference",
            "legacy_notification_preferences_requiring_attribution",
        ),
        (("reports", "0006_report_scope_params_indexes"), "reports", "ReportRun", "report_runs_to_index"),
        (
            ("reports", "0006_report_scope_params_indexes"),
            "reports",
            "ReportSchedule",
            "report_schedules_to_index",
        ),
    )
    for migration, app_label, model_name, evidence_name in row_estimates:
        if not _pending(applied, migration):
            continue
        table = _table(app_label, model_name)
        if _relation_exists(cursor, table):
            estimates[evidence_name] = _scalar(
                cursor,
                f"SELECT count(*) FROM {table}",  # nosec B608
            )


def inspect_schema(applied: set[tuple[str, str]]) -> tuple[dict[str, int], dict[str, int]]:
    """Return aggregate blocking issues and migration workload estimates."""

    issues: dict[str, int] = {}
    estimates: dict[str, int] = {}
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        _academic_checks(cursor, applied, issues, estimates)
        _access_checks(cursor, applied, issues)
        _ai_checks(cursor, applied, issues, estimates)
        _form_checks(cursor, applied, issues)
        _payment_checks(cursor, applied, issues, estimates)
        _money_unit_checks(cursor, applied, issues)
        _organization_checks(cursor, applied, issues, estimates)
        _workload_estimates(cursor, applied, estimates)
    return issues, estimates


class Command(BaseCommand):
    help = "Run read-only data preflights for every pending release-blocking tenant migration."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--fail-on-blocked",
            action="store_true",
            help="Exit nonzero if any tenant would fail a known migration preflight.",
        )

    def handle(self, *args, **options) -> None:
        public_schema = get_public_schema_name()
        Tenant = get_tenant_model()
        with schema_context(public_schema):
            schema_names = list(
                Tenant._default_manager.exclude(schema_name=public_schema)
                .order_by("schema_name")
                .values_list("schema_name", flat=True)
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT table_schema FROM information_schema.tables "
                    "WHERE table_name = 'django_migrations'"
                )
                migration_table_schemas = {row[0] for row in cursor.fetchall()}

        missing = set(schema_names) - migration_table_schemas
        if missing:
            raise CommandError(
                f"Cannot preflight {len(missing)} tenant schema(s) without a local migration table."
            )

        blocked_schemas = 0
        pending_schemas = 0
        total_issues = 0
        for schema_name in schema_names:
            with schema_context(schema_name):
                applied = set(MigrationRecorder(connection).applied_migrations())
                pending = (
                    sorted(REQUIRED_MIGRATIONS - applied) if requires_safeguarding_cutover(applied) else []
                )
                issues, estimates = inspect_schema(applied)
            nonzero_issues = {key: value for key, value in sorted(issues.items()) if value}
            blocked = bool(nonzero_issues)
            blocked_schemas += int(blocked)
            pending_schemas += int(bool(pending))
            total_issues += sum(nonzero_issues.values())
            self.stdout.write(
                json.dumps(
                    {
                        "schema_ref": _schema_ref(schema_name),
                        "pending_maintenance_migrations": [".".join(item) for item in pending],
                        "blocked": blocked,
                        "issues": nonzero_issues,
                        "workload_estimates": dict(sorted(estimates.items())),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

        summary = {
            "schemas": len(schema_names),
            "pending_schemas": pending_schemas,
            "blocked_schemas": blocked_schemas,
            "blocking_issue_count": total_issues,
        }
        self.stdout.write(json.dumps(summary, sort_keys=True, separators=(",", ":")))
        if options["fail_on_blocked"] and blocked_schemas:
            raise CommandError(f"Release migration preflight blocked in {blocked_schemas} tenant schema(s).")
