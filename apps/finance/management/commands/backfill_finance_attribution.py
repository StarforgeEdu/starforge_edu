"""Review and optionally backfill immutable invoice/payment scope snapshots."""

from __future__ import annotations

import json
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.finance.attribution import (
    AttributionResolution,
    ScopeEvidence,
    resolve_scope_evidence,
)
from apps.finance.models import Invoice, PaymentAllocation
from apps.payments.models import Payment
from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES, ScopeAttributionStatus


class Command(BaseCommand):
    help = (
        "Classify legacy invoice/payment branch attribution. Dry-run is the "
        "default; --apply persists only unanimous evidence and records conflicts."
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
            help="Rows examined per query batch (default: 500).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist reviewed resolutions. Without this flag no rows change.",
        )
        parser.add_argument(
            "--quarantine-conflicts",
            action="store_true",
            help="With --apply, quarantine conflicting rows instead of marking conflicting.",
        )
        parser.add_argument(
            "--report",
            help="Optional path for the complete JSON evidence report.",
        )

    def handle(self, *args, **options) -> None:
        batch_size = options["batch_size"]
        if batch_size < 1 or batch_size > 10_000:
            raise CommandError("--batch-size must be between 1 and 10000")
        if options["quarantine_conflicts"] and not options["apply"]:
            raise CommandError("--quarantine-conflicts requires --apply")

        schema_names = self._schema_names(options.get("schema_names"))
        schema_reports: list[dict] = []
        report: dict[str, object] = {
            "mode": "apply" if options["apply"] else "dry_run",
            "schemas": schema_reports,
        }
        for schema_name in schema_names:
            with schema_context(schema_name):
                schema_report = self._process_schema(
                    schema_name,
                    batch_size=batch_size,
                    apply=options["apply"],
                    quarantine_conflicts=options["quarantine_conflicts"],
                )
            schema_reports.append(schema_report)

        report["totals"] = self._totals(schema_reports)
        rendered = json.dumps(report, sort_keys=True, separators=(",", ":"))
        if options.get("report"):
            report_path = Path(options["report"]).expanduser().resolve()
            if not report_path.parent.is_dir():
                raise CommandError(f"Report directory does not exist: {report_path.parent}")
            report_path.write_text(rendered + "\n", encoding="utf-8")
        self.stdout.write(rendered)

    @staticmethod
    def _schema_names(requested: list[str] | None) -> list[str]:
        public_schema = get_public_schema_name()
        Tenant = get_tenant_model()
        with schema_context(public_schema):
            known = set(Tenant.objects.values_list("schema_name", flat=True))
        # Domain tables are tenant-only; never attempt attribution against the
        # public/platform schema even when its Center row shares the tenant model.
        known.discard(public_schema)
        if requested:
            unknown = set(requested) - known
            if unknown:
                raise CommandError(f"Unknown tenant schema(s): {', '.join(sorted(unknown))}")
            return list(dict.fromkeys(requested))
        return sorted(known)

    def _process_schema(
        self,
        schema_name: str,
        *,
        batch_size: int,
        apply: bool,
        quarantine_conflicts: bool,
    ) -> dict:
        invoice_report = self._process_invoices(
            batch_size=batch_size,
            apply=apply,
            quarantine_conflicts=quarantine_conflicts,
        )
        payment_report = self._process_payments(
            batch_size=batch_size,
            apply=apply,
            quarantine_conflicts=quarantine_conflicts,
        )
        return {
            "schema": schema_name,
            "invoices": invoice_report,
            "payments": payment_report,
        }

    def _process_invoices(
        self,
        *,
        batch_size: int,
        apply: bool,
        quarantine_conflicts: bool,
    ) -> dict:
        report = _empty_model_report()
        last_pk = 0
        while True:
            context = transaction.atomic() if apply else nullcontext()
            with context:
                invoices = list(
                    Invoice.objects.filter(pk__gt=last_pk)
                    .select_related(
                        "cohort__department",
                        "department_at_issue",
                    )
                    .order_by("pk")[:batch_size]
                )
                if not invoices:
                    break
                last_pk = invoices[-1].pk
                payment_evidence = self._payment_evidence_for_invoices([invoice.pk for invoice in invoices])
                for invoice in invoices:
                    evidence = self._invoice_evidence(
                        invoice,
                        payment_evidence.get(invoice.pk, ()),
                    )
                    resolution = resolve_scope_evidence(evidence)
                    immutable = invoice.attribution_status in ATTRIBUTED_SCOPE_STATUSES
                    if invoice.attribution_status == ScopeAttributionStatus.QUARANTINED:
                        resolution = AttributionResolution(
                            status=ScopeAttributionStatus.QUARANTINED,
                            branch_id=None,
                            department_id=None,
                            evidence=tuple(evidence),
                        )
                    elif (
                        invoice.attribution_status == ScopeAttributionStatus.CONFLICTING
                        and resolution.status == ScopeAttributionStatus.UNRESOLVED
                    ):
                        resolution = AttributionResolution(
                            status=ScopeAttributionStatus.CONFLICTING,
                            branch_id=None,
                            department_id=None,
                            evidence=tuple(evidence),
                        )
                    if (
                        apply
                        and quarantine_conflicts
                        and not immutable
                        and resolution.status == ScopeAttributionStatus.CONFLICTING
                    ):
                        resolution = AttributionResolution(
                            status=ScopeAttributionStatus.QUARANTINED,
                            branch_id=None,
                            department_id=None,
                            evidence=resolution.evidence,
                        )
                    self._record(
                        report,
                        row_id=invoice.pk,
                        resolution=resolution,
                        immutable=immutable,
                    )
                    if apply and not immutable:
                        self._apply_invoice(
                            invoice.pk,
                            resolution,
                            quarantine_conflicts=quarantine_conflicts,
                        )
        return report

    @staticmethod
    def _invoice_evidence(invoice: Invoice, payment_evidence) -> list[ScopeEvidence]:
        evidence: list[ScopeEvidence] = []
        if invoice.attribution_status in ATTRIBUTED_SCOPE_STATUSES and invoice.branch_at_issue_id is not None:
            department = invoice.department_at_issue
            evidence.append(
                ScopeEvidence(
                    source="stored_snapshot",
                    branch_id=invoice.branch_at_issue_id,
                    department_id=invoice.department_at_issue_id,
                    consistent=(department is None or department.branch_id == invoice.branch_at_issue_id),
                )
            )
        cohort = invoice.cohort
        if cohort is not None:
            department = cohort.department
            evidence.append(
                ScopeEvidence(
                    source="invoice_cohort",
                    branch_id=cohort.branch_id,
                    department_id=cohort.department_id,
                    consistent=(department is None or department.branch_id == cohort.branch_id),
                )
            )
        evidence.extend(payment_evidence)
        return evidence

    @staticmethod
    def _payment_evidence_for_invoices(invoice_ids: list[int]) -> dict[int, tuple[ScopeEvidence, ...]]:
        allocations = list(
            PaymentAllocation.objects.filter(invoice_id__in=invoice_ids).values_list(
                "invoice_id", "payment_id"
            )
        )
        payment_ids = {payment_id for _invoice_id, payment_id in allocations}
        payments = {
            row[0]: row[1:]
            for row in Payment.objects.filter(
                pk__in=payment_ids,
                attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
                branch_at_payment_id__isnull=False,
            ).values_list(
                "pk",
                "branch_at_payment_id",
                "department_at_payment_id",
                "department_at_payment__branch_id",
            )
        }
        evidence: dict[int, list[ScopeEvidence]] = defaultdict(list)
        for invoice_id, payment_id in allocations:
            snapshot = payments.get(payment_id)
            if snapshot is None:
                continue
            branch_id, department_id, department_branch_id = snapshot
            evidence[invoice_id].append(
                ScopeEvidence(
                    source=f"allocated_payment:{payment_id}",
                    branch_id=branch_id,
                    department_id=department_id,
                    consistent=(department_id is None or department_branch_id == branch_id),
                )
            )
        return {key: tuple(value) for key, value in evidence.items()}

    def _process_payments(
        self,
        *,
        batch_size: int,
        apply: bool,
        quarantine_conflicts: bool,
    ) -> dict:
        report = _empty_model_report()
        last_pk = 0
        while True:
            context = transaction.atomic() if apply else nullcontext()
            with context:
                payments = list(
                    Payment.objects.filter(pk__gt=last_pk)
                    .select_related(
                        "cashier_shift",
                        "department_at_payment",
                    )
                    .order_by("pk")[:batch_size]
                )
                if not payments:
                    break
                last_pk = payments[-1].pk
                invoice_evidence = self._invoice_evidence_for_payments(payments)
                for payment in payments:
                    evidence = self._payment_evidence(
                        payment,
                        invoice_evidence.get(payment.pk, ()),
                    )
                    resolution = resolve_scope_evidence(evidence)
                    immutable = payment.attribution_status in ATTRIBUTED_SCOPE_STATUSES
                    if payment.attribution_status == ScopeAttributionStatus.QUARANTINED:
                        resolution = AttributionResolution(
                            status=ScopeAttributionStatus.QUARANTINED,
                            branch_id=None,
                            department_id=None,
                            evidence=tuple(evidence),
                        )
                    elif (
                        payment.attribution_status == ScopeAttributionStatus.CONFLICTING
                        and resolution.status == ScopeAttributionStatus.UNRESOLVED
                    ):
                        resolution = AttributionResolution(
                            status=ScopeAttributionStatus.CONFLICTING,
                            branch_id=None,
                            department_id=None,
                            evidence=tuple(evidence),
                        )
                    if (
                        apply
                        and quarantine_conflicts
                        and not immutable
                        and resolution.status == ScopeAttributionStatus.CONFLICTING
                    ):
                        resolution = AttributionResolution(
                            status=ScopeAttributionStatus.QUARANTINED,
                            branch_id=None,
                            department_id=None,
                            evidence=resolution.evidence,
                        )
                    self._record(
                        report,
                        row_id=payment.pk,
                        resolution=resolution,
                        immutable=immutable,
                    )
                    if apply and not immutable:
                        self._apply_payment(
                            payment.pk,
                            resolution,
                            quarantine_conflicts=quarantine_conflicts,
                        )
        return report

    @staticmethod
    def _invoice_evidence_for_payments(payments: list[Payment]) -> dict[int, tuple[ScopeEvidence, ...]]:
        payment_ids = [payment.pk for payment in payments]
        allocations: dict[int, set[int]] = defaultdict(set)
        for payment_id, invoice_id in PaymentAllocation.objects.filter(
            payment_id__in=payment_ids
        ).values_list("payment_id", "invoice_id"):
            allocations[payment_id].add(invoice_id)

        metadata_ids: dict[int, int] = {}
        account_refs: dict[int, str] = {}
        for payment in payments:
            raw_invoice_id = payment.metadata.get("invoice_id")
            try:
                if raw_invoice_id is not None and not isinstance(raw_invoice_id, bool):
                    metadata_ids[payment.pk] = int(raw_invoice_id)
            except (TypeError, ValueError):
                pass
            if payment.account_ref:
                account_refs[payment.pk] = payment.account_ref

        invoice_ids = set(metadata_ids.values())
        for values in allocations.values():
            invoice_ids.update(values)
        invoices = list(
            Invoice.objects.filter(
                models_q_for_invoice_references(
                    invoice_ids=invoice_ids,
                    invoice_numbers=set(account_refs.values()),
                )
            ).select_related("cohort__department", "department_at_issue")
        )
        by_id = {invoice.pk: invoice for invoice in invoices}
        by_number = {invoice.number: invoice for invoice in invoices}

        evidence: dict[int, list[ScopeEvidence]] = defaultdict(list)
        for payment in payments:
            references: list[tuple[str, Invoice | None]] = []
            if payment.pk in metadata_ids:
                references.append(("metadata_invoice", by_id.get(metadata_ids[payment.pk])))
            if payment.pk in account_refs:
                references.append(("account_ref", by_number.get(account_refs[payment.pk])))
            references.extend(
                (f"allocation:{invoice_id}", by_id.get(invoice_id))
                for invoice_id in allocations.get(payment.pk, set())
            )
            for source, invoice in references:
                if invoice is None:
                    continue
                resolution = resolve_scope_evidence(Command._invoice_evidence(invoice, ()))
                if resolution.status != ScopeAttributionStatus.RESOLVED:
                    continue
                if resolution.branch_id is None:
                    raise CommandError(f"Resolved attribution for invoice {invoice.pk} has no branch.")
                evidence[payment.pk].append(
                    ScopeEvidence(
                        source=f"{source}:{invoice.pk}",
                        branch_id=resolution.branch_id,
                        department_id=resolution.department_id,
                    )
                )
        return {key: tuple(value) for key, value in evidence.items()}

    @staticmethod
    def _payment_evidence(payment: Payment, invoice_evidence) -> list[ScopeEvidence]:
        evidence: list[ScopeEvidence] = []
        if (
            payment.attribution_status in ATTRIBUTED_SCOPE_STATUSES
            and payment.branch_at_payment_id is not None
        ):
            department = payment.department_at_payment
            evidence.append(
                ScopeEvidence(
                    source="stored_snapshot",
                    branch_id=payment.branch_at_payment_id,
                    department_id=payment.department_at_payment_id,
                    consistent=(department is None or department.branch_id == payment.branch_at_payment_id),
                )
            )
        cashier_shift = payment.cashier_shift
        if cashier_shift is not None:
            evidence.append(
                ScopeEvidence(
                    source=f"cashier_shift:{payment.cashier_shift_id}",
                    branch_id=cashier_shift.branch_id,
                )
            )
        evidence.extend(invoice_evidence)
        return evidence

    @staticmethod
    def _record(
        report: dict,
        *,
        row_id: int,
        resolution: AttributionResolution,
        immutable: bool,
    ) -> None:
        status = str(resolution.status)
        report[status] += 1
        # Report every candidate that may change plus every conflict requiring
        # review. Do not materialize millions of already-captured, still-
        # consistent rows into memory merely to repeat their IDs.
        if status != ScopeAttributionStatus.RESOLVED or not immutable:
            report["review"].append(
                {
                    "id": row_id,
                    "immutable": immutable,
                    **resolution.as_dict(),
                }
            )

    @staticmethod
    def _apply_invoice(
        invoice_id: int,
        resolution: AttributionResolution,
        *,
        quarantine_conflicts: bool,
    ) -> None:
        values = _update_values(
            resolution,
            branch_field="branch_at_issue_id",
            department_field="department_at_issue_id",
            quarantine_conflicts=quarantine_conflicts,
        )
        Invoice.objects.filter(
            pk=invoice_id,
            attribution_status__in=(
                ScopeAttributionStatus.UNRESOLVED,
                ScopeAttributionStatus.CONFLICTING,
            ),
        ).update(**values)

    @staticmethod
    def _apply_payment(
        payment_id: int,
        resolution: AttributionResolution,
        *,
        quarantine_conflicts: bool,
    ) -> None:
        values = _update_values(
            resolution,
            branch_field="branch_at_payment_id",
            department_field="department_at_payment_id",
            quarantine_conflicts=quarantine_conflicts,
        )
        Payment.objects.filter(
            pk=payment_id,
            attribution_status__in=(
                ScopeAttributionStatus.UNRESOLVED,
                ScopeAttributionStatus.CONFLICTING,
            ),
        ).update(**values)

    @staticmethod
    def _totals(schema_reports: list[dict]) -> dict:
        totals = {
            "invoices": _empty_counts(),
            "payments": _empty_counts(),
        }
        for schema in schema_reports:
            for model_name in ("invoices", "payments"):
                for status in totals[model_name]:
                    totals[model_name][status] += schema[model_name][status]
        return totals


def models_q_for_invoice_references(*, invoice_ids: set[int], invoice_numbers: set[str]):
    from django.db.models import Q

    predicate = Q(pk__in=invoice_ids)
    if invoice_numbers:
        predicate |= Q(number__in=invoice_numbers)
    return predicate


def _update_values(
    resolution: AttributionResolution,
    *,
    branch_field: str,
    department_field: str,
    quarantine_conflicts: bool,
) -> dict:
    if resolution.status == ScopeAttributionStatus.RESOLVED:
        return {
            branch_field: resolution.branch_id,
            department_field: resolution.department_id,
            "attribution_status": ScopeAttributionStatus.RESOLVED,
        }
    if resolution.status == ScopeAttributionStatus.CONFLICTING:
        return {
            branch_field: None,
            department_field: None,
            "attribution_status": (
                ScopeAttributionStatus.QUARANTINED
                if quarantine_conflicts
                else ScopeAttributionStatus.CONFLICTING
            ),
        }
    return {
        branch_field: None,
        department_field: None,
        "attribution_status": resolution.status,
    }


def _empty_counts() -> dict[str, int]:
    return {
        str(ScopeAttributionStatus.RESOLVED): 0,
        str(ScopeAttributionStatus.UNRESOLVED): 0,
        str(ScopeAttributionStatus.CONFLICTING): 0,
        str(ScopeAttributionStatus.QUARANTINED): 0,
    }


def _empty_model_report() -> dict:
    return {**_empty_counts(), "review": []}
