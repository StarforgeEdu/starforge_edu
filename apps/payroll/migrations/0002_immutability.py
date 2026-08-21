from django.db import migrations, models


FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION payroll_reject_row_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER payroll_line_item_append_only
BEFORE UPDATE OR DELETE ON payroll_payrolllineitem
FOR EACH ROW EXECUTE FUNCTION payroll_reject_row_mutation();

CREATE TRIGGER payroll_payslip_append_only
BEFORE UPDATE OR DELETE ON payroll_payrollpayslip
FOR EACH ROW EXECUTE FUNCTION payroll_reject_row_mutation();

CREATE OR REPLACE FUNCTION payroll_protect_period_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'payroll period events are append-only' USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.period_id, OLD.action, OLD.actor_principal_kind,
        OLD.actor_principal_id, OLD.note, OLD.idempotency_key_hash,
        OLD.operation_fingerprint, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.period_id, NEW.action, NEW.actor_principal_kind,
        NEW.actor_principal_id, NEW.note, NEW.idempotency_key_hash,
        NEW.operation_fingerprint, NEW.created_at
    ) OR NOT (
        NEW.actor_id IS NOT DISTINCT FROM OLD.actor_id
        OR (OLD.actor_id IS NOT NULL AND NEW.actor_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll period events are append-only' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_period_event_append_only
BEFORE UPDATE OR DELETE ON payroll_payrollperiodevent
FOR EACH ROW EXECUTE FUNCTION payroll_protect_period_event();

CREATE OR REPLACE FUNCTION payroll_protect_adjustment_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'payroll adjustment events are append-only' USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.adjustment_id, OLD.action, OLD.actor_principal_kind,
        OLD.actor_principal_id, OLD.note, OLD.idempotency_key_hash,
        OLD.operation_fingerprint, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.adjustment_id, NEW.action, NEW.actor_principal_kind,
        NEW.actor_principal_id, NEW.note, NEW.idempotency_key_hash,
        NEW.operation_fingerprint, NEW.created_at
    ) OR NOT (
        NEW.actor_id IS NOT DISTINCT FROM OLD.actor_id
        OR (OLD.actor_id IS NOT NULL AND NEW.actor_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll adjustment events are append-only' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_adjustment_event_append_only
BEFORE UPDATE OR DELETE ON payroll_payrolladjustmentevent
FOR EACH ROW EXECUTE FUNCTION payroll_protect_adjustment_event();

CREATE OR REPLACE FUNCTION payroll_protect_reconciliation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'payroll reconciliations are append-only' USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.line_item_id, OLD.kind, OLD.reverses_id, OLD.amount_uzs,
        OLD.currency, OLD.payment_method_id, OLD.external_reference,
        OLD.paid_at, OLD.reason, OLD.ledger_entry_id,
        OLD.recorded_principal_kind, OLD.recorded_principal_id,
        OLD.idempotency_key_hash, OLD.operation_fingerprint, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.line_item_id, NEW.kind, NEW.reverses_id, NEW.amount_uzs,
        NEW.currency, NEW.payment_method_id, NEW.external_reference,
        NEW.paid_at, NEW.reason, NEW.ledger_entry_id,
        NEW.recorded_principal_kind, NEW.recorded_principal_id,
        NEW.idempotency_key_hash, NEW.operation_fingerprint, NEW.created_at
    ) OR NOT (
        NEW.recorded_by_id IS NOT DISTINCT FROM OLD.recorded_by_id
        OR (OLD.recorded_by_id IS NOT NULL AND NEW.recorded_by_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll reconciliations are append-only' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_reconciliation_append_only
BEFORE UPDATE OR DELETE ON payroll_payrollreconciliation
FOR EACH ROW EXECUTE FUNCTION payroll_protect_reconciliation();

CREATE OR REPLACE FUNCTION payroll_protect_adjustment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'payroll adjustments cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.teacher_id, OLD.branch_id, OLD.department_id, OLD.kind,
        OLD.amount_uzs, OLD.currency, OLD.effective_period_start,
        OLD.effective_period_end, OLD.reason,
        OLD.created_principal_kind, OLD.created_principal_id,
        OLD.idempotency_key_hash, OLD.operation_fingerprint, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.teacher_id, NEW.branch_id, NEW.department_id, NEW.kind,
        NEW.amount_uzs, NEW.currency, NEW.effective_period_start,
        NEW.effective_period_end, NEW.reason,
        NEW.created_principal_kind, NEW.created_principal_id,
        NEW.idempotency_key_hash, NEW.operation_fingerprint, NEW.created_at
    ) THEN
        RAISE EXCEPTION 'payroll adjustment evidence is immutable' USING ERRCODE = '55000';
    END IF;
    IF NOT (
        NEW.created_by_id IS NOT DISTINCT FROM OLD.created_by_id
        OR (OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll adjustment creator cannot change' USING ERRCODE = '55000';
    END IF;
    IF NEW.decided_by_id IS DISTINCT FROM OLD.decided_by_id AND NOT (
        (OLD.state = 'pending' AND NEW.state IN ('approved', 'rejected') AND OLD.decided_by_id IS NULL)
        OR (OLD.decided_by_id IS NOT NULL AND NEW.decided_by_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll adjustment decider cannot change' USING ERRCODE = '55000';
    END IF;
    IF NOT (
        NEW.state = OLD.state
        OR (OLD.state = 'pending' AND NEW.state IN ('approved', 'rejected'))
        OR (OLD.state = 'approved' AND NEW.state = 'applied')
        OR (OLD.state = 'applied' AND NEW.state = 'approved')
    ) THEN
        RAISE EXCEPTION 'invalid payroll adjustment transition' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_adjustment_protect
BEFORE UPDATE OR DELETE ON payroll_payrolladjustment
FOR EACH ROW EXECUTE FUNCTION payroll_protect_adjustment();

CREATE OR REPLACE FUNCTION payroll_protect_period()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'payroll periods cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.branch_id, OLD.department_id, OLD.label, OLD.period_start,
        OLD.period_end, OLD.pay_date, OLD.currency, OLD.organization_timezone, OLD.correction_of_id,
        OLD.correction_reason, OLD.created_principal_kind,
        OLD.created_principal_id, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.branch_id, NEW.department_id, NEW.label, NEW.period_start,
        NEW.period_end, NEW.pay_date, NEW.currency, NEW.organization_timezone, NEW.correction_of_id,
        NEW.correction_reason, NEW.created_principal_kind,
        NEW.created_principal_id, NEW.created_at
    ) THEN
        RAISE EXCEPTION 'payroll period identity is immutable' USING ERRCODE = '55000';
    END IF;
    IF NOT (
        NEW.created_by_id IS NOT DISTINCT FROM OLD.created_by_id
        OR (OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll period creator cannot change' USING ERRCODE = '55000';
    END IF;
    IF OLD.status <> 'draft' AND ROW(
        OLD.run_principal_kind, OLD.run_principal_id,
        OLD.run_idempotency_key_hash, OLD.run_fingerprint, OLD.line_count,
        OLD.base_total_uzs, OLD.bonus_total_uzs, OLD.deduction_total_uzs,
        OLD.net_total_uzs, OLD.frozen_at
    ) IS DISTINCT FROM ROW(
        NEW.run_principal_kind, NEW.run_principal_id,
        NEW.run_idempotency_key_hash, NEW.run_fingerprint, NEW.line_count,
        NEW.base_total_uzs, NEW.bonus_total_uzs, NEW.deduction_total_uzs,
        NEW.net_total_uzs, NEW.frozen_at
    ) THEN
        RAISE EXCEPTION 'frozen payroll totals are immutable' USING ERRCODE = '55000';
    END IF;
    IF NEW.run_by_id IS DISTINCT FROM OLD.run_by_id AND NOT (
        (OLD.status = 'draft' AND NEW.status = 'pending_approval' AND OLD.run_by_id IS NULL)
        OR (OLD.run_by_id IS NOT NULL AND NEW.run_by_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll runner cannot change' USING ERRCODE = '55000';
    END IF;
    IF NEW.approved_by_id IS DISTINCT FROM OLD.approved_by_id AND NOT (
        (OLD.status = 'pending_approval' AND NEW.status IN ('approved', 'paid') AND OLD.approved_by_id IS NULL)
        OR (OLD.approved_by_id IS NOT NULL AND NEW.approved_by_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll approver cannot change' USING ERRCODE = '55000';
    END IF;
    IF NEW.rejected_by_id IS DISTINCT FROM OLD.rejected_by_id AND NOT (
        (OLD.status = 'pending_approval' AND NEW.status = 'rejected' AND OLD.rejected_by_id IS NULL)
        OR (OLD.rejected_by_id IS NOT NULL AND NEW.rejected_by_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll rejector cannot change' USING ERRCODE = '55000';
    END IF;
    IF NOT (
        NEW.status = OLD.status
        OR (OLD.status = 'draft' AND NEW.status = 'pending_approval')
        OR (OLD.status = 'pending_approval' AND NEW.status IN ('approved', 'rejected', 'paid'))
        OR (OLD.status = 'approved' AND NEW.status IN ('payment_in_progress', 'paid'))
        OR (OLD.status = 'payment_in_progress' AND NEW.status IN ('approved', 'paid'))
        OR (OLD.status = 'paid' AND NEW.status IN ('approved', 'payment_in_progress'))
    ) THEN
        RAISE EXCEPTION 'invalid payroll period transition' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_period_protect
BEFORE UPDATE OR DELETE ON payroll_payrollperiod
FOR EACH ROW EXECUTE FUNCTION payroll_protect_period();

CREATE OR REPLACE FUNCTION payroll_protect_export()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'payroll export evidence cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.period_id, OLD.format, OLD.filters,
        OLD.requested_principal_kind, OLD.requested_principal_id,
        OLD.idempotency_key_hash, OLD.operation_fingerprint, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.period_id, NEW.format, NEW.filters,
        NEW.requested_principal_kind, NEW.requested_principal_id,
        NEW.idempotency_key_hash, NEW.operation_fingerprint, NEW.created_at
    ) THEN
        RAISE EXCEPTION 'payroll export request is immutable' USING ERRCODE = '55000';
    END IF;
    IF NOT (
        NEW.requested_by_id IS NOT DISTINCT FROM OLD.requested_by_id
        OR (OLD.requested_by_id IS NOT NULL AND NEW.requested_by_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'payroll export requester cannot change' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_export_protect
BEFORE UPDATE OR DELETE ON payroll_payrollexport
FOR EACH ROW EXECUTE FUNCTION payroll_protect_export();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS payroll_export_protect ON payroll_payrollexport;
DROP TRIGGER IF EXISTS payroll_period_protect ON payroll_payrollperiod;
DROP TRIGGER IF EXISTS payroll_adjustment_protect ON payroll_payrolladjustment;
DROP TRIGGER IF EXISTS payroll_reconciliation_append_only ON payroll_payrollreconciliation;
DROP TRIGGER IF EXISTS payroll_adjustment_event_append_only ON payroll_payrolladjustmentevent;
DROP TRIGGER IF EXISTS payroll_period_event_append_only ON payroll_payrollperiodevent;
DROP TRIGGER IF EXISTS payroll_payslip_append_only ON payroll_payrollpayslip;
DROP TRIGGER IF EXISTS payroll_line_item_append_only ON payroll_payrolllineitem;
DROP FUNCTION IF EXISTS payroll_protect_export();
DROP FUNCTION IF EXISTS payroll_protect_period();
DROP FUNCTION IF EXISTS payroll_protect_adjustment();
DROP FUNCTION IF EXISTS payroll_protect_reconciliation();
DROP FUNCTION IF EXISTS payroll_protect_adjustment_event();
DROP FUNCTION IF EXISTS payroll_protect_period_event();
DROP FUNCTION IF EXISTS payroll_reject_row_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("payroll", "0001_initial")]

    operations = [
        migrations.AddConstraint(
            model_name="payrollperiod",
            constraint=models.CheckConstraint(
                condition=models.Q(currency="UZS"), name="pay_period_currency_uzs"
            ),
        ),
        migrations.AddConstraint(
            model_name="payrolllineitem",
            constraint=models.CheckConstraint(
                condition=models.Q(currency="UZS"), name="pay_line_currency_uzs"
            ),
        ),
        migrations.AddConstraint(
            model_name="payrolladjustment",
            constraint=models.CheckConstraint(
                condition=models.Q(currency="UZS"), name="pay_adjust_currency_uzs"
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollreconciliation",
            constraint=models.CheckConstraint(
                condition=models.Q(currency="UZS"), name="pay_recon_currency_uzs"
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollperiod",
            constraint=models.UniqueConstraint(
                condition=models.Q(correction_of__isnull=False),
                fields=("correction_of",),
                name="pay_period_one_correction",
            ),
        ),
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
