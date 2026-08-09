"""Audited recovery contracts for every StarForge Celery task family.

Late acknowledgement is safe only when redelivery has a deliberate domain
invariant.  This registry is executable release documentation: registration
tests require every project task to match exactly one non-overlapping rule, so
adding a task without reviewing its duplicate/outcome behavior fails CI.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class DurabilityContract:
    pattern: str
    invariant: str
    recovery: str


TASK_DURABILITY_CONTRACTS = (
    DurabilityContract(
        "celery_tasks.academics_tasks.generate_transcript_pdf",
        "advisory execution lock plus deterministic transcript object key and terminal row state",
        "redelivery observes DONE or retries the same transcript row",
    ),
    DurabilityContract(
        "celery_tasks.ai_tasks.run_*",
        "AIRequest row claim, task-id ownership, advisory execution lock, and provider-ambiguity quarantine",
        "redelivery resumes the same request or requires operator reconciliation after unknown acceptance",
    ),
    DurabilityContract(
        "celery_tasks.ai_tasks.purge_expired_ai_content",
        "bounded idempotent database redaction",
        "a later sweep safely repeats incomplete rows",
    ),
    DurabilityContract(
        "celery_tasks.assignment_tasks.*",
        "tenant fan-out plus notification domain dedupe keys",
        "duplicate sweeps converge on the same reminder notifications",
    ),
    DurabilityContract(
        "celery_tasks.attachment_tasks.*",
        "locked upload-grant state and idempotent object deletion",
        "grant timestamps expose unfinished cleanup for the next bounded sweep",
    ),
    DurabilityContract(
        "celery_tasks.attendance_tasks.*",
        "conditional attendance transitions and notification dedupe",
        "a later sweep repeats only still-eligible lesson rows",
    ),
    DurabilityContract(
        "celery_tasks.audit_tasks.*",
        "retention predicates over immutable audit rows under a transaction-local maintenance capability",
        "repeated deletion is idempotent",
    ),
    DurabilityContract(
        "celery_tasks.billing_tasks.*",
        "period-keyed usage snapshots and conditional subscription transitions",
        "rerun converges on the same metering period",
    ),
    DurabilityContract(
        "celery_tasks.campaign_tasks.dispatch_scheduled_campaigns*",
        "durable campaign claim before child publication",
        "stale claim recovery republishes only unfinished campaigns",
    ),
    DurabilityContract(
        "celery_tasks.campaign_tasks.deliver_campaign",
        "campaign lease, advisory mutex, and recipient compare-and-swap before paid SMS",
        "ambiguous provider outcomes remain at-most-once and operator-reviewable",
    ),
    DurabilityContract(
        "celery_tasks.cleanup_tasks.*",
        "date predicate over disposable OTP rows",
        "repeated deletion is idempotent",
    ),
    DurabilityContract(
        "celery_tasks.content_tasks.*",
        "content status transitions, trusted tenant key grammar, and deterministic durable object keys",
        "row state or an idempotent object operation makes redelivery converge",
    ),
    DurabilityContract(
        "celery_tasks.finance_tasks.generate_statement_pdf",
        "locked durable StatementExport lifecycle row, immutable invoice snapshot, and deterministic tenant object key",
        "redelivery observes terminal state or retries the same export; an upload-before-commit crash retains row ownership of the only key",
    ),
    DurabilityContract(
        "celery_tasks.finance_tasks.maintain_statement_exports",
        "public dispatcher fans out maintenance only to active tenant schemas",
        "the next periodic sweep republishes tenant-local maintenance without processing tenant rows in public",
    ),
    DurabilityContract(
        "celery_tasks.finance_tasks.maintain_statement_exports_for_schema",
        "bounded durable StatementExport expiry and stale-delivery recovery under tenant-local execution",
        "later sweeps retry cleanup and republish only still-queued or stale-running exports",
    ),
    DurabilityContract(
        "celery_tasks.finance_tasks.late_payment_reminders*",
        "invoice-cycle notification dedupe",
        "repeated scans converge on the same reminder cycle",
    ),
    DurabilityContract(
        "celery_tasks.finance_tasks.refresh_fx_rate*",
        "replace-only tenant cache snapshot",
        "a retry or next daily sweep refreshes the same currency pair",
    ),
    DurabilityContract(
        "celery_tasks.health_tasks.*",
        "replace-only expiring heartbeat key",
        "the next beat tick supersedes a lost heartbeat",
    ),
    DurabilityContract(
        "celery_tasks.notification_tasks.*",
        "principal-scoped Notification dedupe, committed channel outcomes, and durable quiet-hours outbox markers",
        "cutover draining preserves work, but an adapter-success-before-delivery-row crash remains ambiguous until channels gain a durable pre-send claim or provider idempotency",
    ),
    DurabilityContract(
        "celery_tasks.payment_tasks.fiscalize_payment",
        "payment-scoped fiscal outbox lease and provider idempotency",
        "stale/failed receipt reconciliation republishes the same payment",
    ),
    DurabilityContract(
        "celery_tasks.payment_tasks.reconcile_fiscal_receipts*",
        "bounded fiscal-outbox state scan",
        "duplicate reconciliation publishes idempotent payment work",
    ),
    DurabilityContract(
        "celery_tasks.payment_tasks.generate_receipt_pdf",
        "confirmed receipt state and deterministic payment receipt object key",
        "redelivery returns the trusted existing key",
    ),
    DurabilityContract(
        "celery_tasks.payment_tasks.prune_webhook_events*",
        "retention predicate over already-processed replay evidence",
        "repeated deletion is idempotent",
    ),
    DurabilityContract(
        "celery_tasks.payroll_tasks.*",
        "payroll export row lock, execution mutex, deterministic object key, and terminal audit",
        "retry resets only non-terminal exports and redelivery reuses DONE",
    ),
    DurabilityContract(
        "celery_tasks.print_tasks.*",
        "durable print-job state/audit dedupe; physical output is pulled under a separate delivery lease",
        "Celery never reprints; stale physical leases are quarantined for reconciliation",
    ),
    DurabilityContract(
        "celery_tasks.report_tasks.build_report",
        "report-run row state, advisory execution lock, and deterministic artifact ownership",
        "retry resets only the same non-terminal run",
    ),
    DurabilityContract(
        "celery_tasks.report_tasks.run_due_report_schedules*",
        "schedule bucket and last-run guard",
        "duplicate scans converge on the same report run",
    ),
    DurabilityContract(
        "celery_tasks.schedule_tasks.*",
        "conditional schedule transitions and event-specific notification dedupe",
        "later sweeps repeat only still-due domain rows",
    ),
    DurabilityContract(
        "celery_tasks.tenancy_tasks.*",
        "locked active/trial predicate plus one-way center transition",
        "redelivery skips already-deactivated centers",
    ),
)


def contract_for_task(task_name: str) -> DurabilityContract:
    matches = [
        contract for contract in TASK_DURABILITY_CONTRACTS if fnmatch.fnmatchcase(task_name, contract.pattern)
    ]
    if len(matches) != 1:
        raise ValueError(f"Task {task_name!r} matched {len(matches)} durability contracts.")
    return matches[0]


def registry_fingerprint(task_names: list[str]) -> str:
    manifest = "\n".join(
        f"{name}\0{contract_for_task(name).pattern}\0{contract_for_task(name).invariant}"
        for name in sorted(task_names)
    )
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()
