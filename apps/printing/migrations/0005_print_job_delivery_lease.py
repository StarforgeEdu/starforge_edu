"""Quarantine physical-print attempts behind explicit delivery leases.

Existing PICKED/PRINTING rows have no per-attempt lease and their physical
outcome cannot be inferred safely during deployment.  They are conservatively
moved to reconciliation_required with an expired synthetic lease.  No row is
requeued and no physical output is guessed.
"""

from __future__ import annotations

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


RECONCILIATION_IMMUTABILITY_SQL = r"""
CREATE OR REPLACE FUNCTION printing_reconciliation_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Evidence must describe the exact quarantined attempt that exists at
        -- insertion time.  This prevents direct ORM/SQL writers from forging a
        -- branch snapshot or attaching evidence to a later/different lease.
        PERFORM 1
          FROM printing_printjob
         WHERE id = NEW.job_id
           AND branch_id = NEW.branch_id
           AND lease_id = NEW.lease_id
           AND status = 'reconciliation_required'
           AND reconciliation_previous_status = NEW.previous_status
           AND reconciliation_reason = NEW.reason;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'print reconciliation does not match the quarantined attempt'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    -- Preserve evidence if the resolving user is deleted. This SET NULL FK
    -- maintenance is the only content-neutral update the ledger permits.
    IF TG_OP = 'UPDATE' THEN
        IF OLD.resolved_by_id IS NOT NULL
           AND NEW.resolved_by_id IS NULL
           AND (to_jsonb(NEW) - 'resolved_by_id') = (to_jsonb(OLD) - 'resolved_by_id') THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'print reconciliation evidence is append-only'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS print_reconciliation_immutable
    ON printing_printjobreconciliation;
CREATE TRIGGER print_reconciliation_immutable
BEFORE INSERT OR UPDATE OR DELETE ON printing_printjobreconciliation
FOR EACH ROW EXECUTE FUNCTION printing_reconciliation_reject_mutation();
"""

RECONCILIATION_IMMUTABILITY_REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS print_reconciliation_immutable
    ON printing_printjobreconciliation;
DROP FUNCTION IF EXISTS printing_reconciliation_reject_mutation();
"""


def quarantine_legacy_inflight_jobs(apps, schema_editor):
    PrintJob = apps.get_model("printing", "PrintJob")
    now = timezone.now()
    rows = PrintJob.objects.filter(status__in=("picked", "printing")).only(
        "pk",
        "claimed_at",
        "status",
    )
    for job in rows.iterator(chunk_size=500):
        heartbeat = job.claimed_at or now
        PrintJob.objects.filter(pk=job.pk).update(
            status="reconciliation_required",
            lease_id=uuid.uuid4(),
            last_heartbeat_at=heartbeat,
            lease_expires_at=now,
            reconciliation_required_at=now,
            reconciliation_reason="legacy_unleased",
            reconciliation_previous_status=job.status,
            next_attempt_at=None,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("org", "0021_durable_center_settings"),
        ("printing", "0004_printjob_unique_open_source"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PrintJobReconciliation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("lease_id", models.UUIDField(default=uuid.uuid4, editable=False)),
                (
                    "previous_status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("picked", "Picked"),
                            ("printing", "Printing"),
                            ("reconciliation_required", "Reconciliation required"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                        ],
                        editable=False,
                        max_length=32,
                    ),
                ),
                (
                    "outcome",
                    models.CharField(
                        choices=[
                            ("confirmed_printed", "Confirmed printed"),
                            ("confirmed_not_printed", "Confirmed not printed"),
                            ("abandoned_unknown", "Abandoned with unknown output"),
                        ],
                        editable=False,
                        max_length=32,
                    ),
                ),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("lease_expired", "Lease expired"),
                            ("legacy_unleased", "Legacy claim had no lease"),
                            ("agent_reported_failure", "Agent reported ambiguous failure"),
                        ],
                        editable=False,
                        max_length=32,
                    ),
                ),
                ("evidence_reference", models.CharField(editable=False, max_length=200)),
                ("pages_printed", models.PositiveIntegerField(default=0, editable=False)),
                ("attempts", models.PositiveSmallIntegerField(default=0, editable=False)),
                (
                    "agent_id_at_resolution",
                    models.PositiveBigIntegerField(blank=True, editable=False, null=True),
                ),
                (
                    "printer_id_at_resolution",
                    models.PositiveBigIntegerField(blank=True, editable=False, null=True),
                ),
                (
                    "idempotency_key_hash",
                    models.CharField(editable=False, max_length=64, unique=True),
                ),
                ("resolved_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ("-resolved_at", "-id")},
        ),
        migrations.RemoveConstraint(
            model_name="printjob",
            name="printing_unique_open_source",
        ),
        migrations.AddField(
            model_name="printjob",
            name="last_heartbeat_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="last heartbeat at",
            ),
        ),
        migrations.AddField(
            model_name="printjob",
            name="lease_expires_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="lease expires at",
            ),
        ),
        migrations.AddField(
            model_name="printjob",
            name="lease_id",
            field=models.UUIDField(
                blank=True,
                editable=False,
                null=True,
                unique=True,
                verbose_name="lease id",
            ),
        ),
        migrations.AddField(
            model_name="printjob",
            name="reconciliation_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("lease_expired", "Lease expired"),
                    ("legacy_unleased", "Legacy claim had no lease"),
                    ("agent_reported_failure", "Agent reported ambiguous failure"),
                ],
                default="",
                max_length=32,
                verbose_name="reconciliation reason",
            ),
        ),
        migrations.AddField(
            model_name="printjob",
            name="reconciliation_required_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="reconciliation required at",
            ),
        ),
        migrations.AddField(
            model_name="printjob",
            name="reconciliation_previous_status",
            field=models.CharField(
                blank=True,
                choices=[("picked", "Picked"), ("printing", "Printing")],
                default="",
                max_length=16,
                verbose_name="reconciliation previous status",
            ),
        ),
        migrations.AlterField(
            model_name="printjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("picked", "Picked"),
                    ("printing", "Printing"),
                    ("reconciliation_required", "Reconciliation required"),
                    ("done", "Done"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="queued",
                max_length=32,
                verbose_name="status",
            ),
        ),
        migrations.RunPython(
            quarantine_legacy_inflight_jobs,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddIndex(
            model_name="printjob",
            index=models.Index(
                fields=["status", "lease_expires_at", "id"],
                name="printjob_stale_lease_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="printjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    status__in=(
                        "queued",
                        "picked",
                        "printing",
                        "reconciliation_required",
                    )
                ),
                fields=("branch", "source", "source_id", "payload_s3_key"),
                name="printing_unique_open_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="printjob",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        status__in=("picked", "printing"),
                        agent__isnull=False,
                        lease_id__isnull=False,
                        last_heartbeat_at__isnull=False,
                        lease_expires_at__isnull=False,
                        reconciliation_required_at__isnull=True,
                        reconciliation_reason="",
                        reconciliation_previous_status="",
                    )
                    | (
                        models.Q(
                            status="reconciliation_required",
                            lease_id__isnull=False,
                            last_heartbeat_at__isnull=False,
                            lease_expires_at__isnull=False,
                            reconciliation_required_at__isnull=False,
                            reconciliation_reason__in=(
                                "lease_expired",
                                "legacy_unleased",
                                "agent_reported_failure",
                            ),
                            reconciliation_previous_status__in=("picked", "printing"),
                        )
                    )
                    | models.Q(
                        status__in=("queued", "done", "failed"),
                        lease_id__isnull=True,
                        last_heartbeat_at__isnull=True,
                        lease_expires_at__isnull=True,
                        reconciliation_required_at__isnull=True,
                        reconciliation_reason="",
                        reconciliation_previous_status="",
                    )
                ),
                name="printjob_lease_state_ck",
            ),
        ),
        migrations.AddField(
            model_name="printjobreconciliation",
            name="branch",
            field=models.ForeignKey(
                editable=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="print_job_reconciliations",
                to="org.branch",
            ),
        ),
        migrations.AddField(
            model_name="printjobreconciliation",
            name="job",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reconciliations",
                to="printing.printjob",
            ),
        ),
        migrations.AddField(
            model_name="printjobreconciliation",
            name="resolved_by",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="resolved_print_reconciliations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="printjobreconciliation",
            index=models.Index(
                fields=["branch", "-resolved_at", "id"],
                name="printrecon_branch_time_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="printjobreconciliation",
            constraint=models.UniqueConstraint(
                fields=("job", "lease_id"),
                name="print_reconcile_job_lease_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="printjobreconciliation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    outcome__in=(
                        "confirmed_printed",
                        "confirmed_not_printed",
                        "abandoned_unknown",
                    )
                ),
                name="print_reconcile_outcome_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="printjobreconciliation",
            constraint=models.CheckConstraint(
                condition=models.Q(previous_status__in=("picked", "printing")),
                name="print_reconcile_previous_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="printjobreconciliation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    reason__in=(
                        "lease_expired",
                        "legacy_unleased",
                        "agent_reported_failure",
                    )
                ),
                name="print_reconcile_reason_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="printjobreconciliation",
            constraint=models.CheckConstraint(
                condition=~models.Q(evidence_reference=""),
                name="print_reconcile_evidence_ck",
            ),
        ),
        migrations.RunSQL(
            RECONCILIATION_IMMUTABILITY_SQL,
            RECONCILIATION_IMMUTABILITY_REVERSE_SQL,
        ),
    ]
