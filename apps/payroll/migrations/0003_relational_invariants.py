from django.db import migrations


FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION payroll_principal_matches(
    principal_kind text,
    principal_id bigint,
    bridge_user_id bigint
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    IF principal_kind = 'staff' THEN
        RETURN EXISTS (
            SELECT 1
            FROM org_staffprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = principal_id
              AND profile.user_id = bridge_user_id
              AND profile.is_active
              AND bridge.is_active
        );
    ELSIF principal_kind = 'teacher' THEN
        RETURN EXISTS (
            SELECT 1
            FROM teachers_teacherprofile profile
            JOIN users_user bridge ON bridge.id = profile.user_id
            WHERE profile.id = principal_id
              AND profile.user_id = bridge_user_id
              AND profile.is_active
              AND bridge.is_active
        );
    END IF;
    RETURN FALSE;
END;
$$;

CREATE OR REPLACE FUNCTION payroll_validate_period_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    original payroll_payrollperiod%ROWTYPE;
BEGIN
    -- One lock order and one serialization point for every overlapping scope.
    PERFORM 1 FROM org_branch WHERE id = NEW.branch_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'payroll branch is unavailable' USING ERRCODE = '23503';
    END IF;
    IF NEW.department_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM org_department department
        WHERE department.id = NEW.department_id
          AND department.branch_id = NEW.branch_id
    ) THEN
        RAISE EXCEPTION 'payroll department is outside branch' USING ERRCODE = '23514';
    END IF;
    IF NEW.status <> 'draft'
       OR NEW.line_count <> 0
       OR NEW.base_total_uzs <> 0
       OR NEW.bonus_total_uzs <> 0
       OR NEW.deduction_total_uzs <> 0
       OR NEW.net_total_uzs <> 0
       OR NEW.paid_total_uzs <> 0
       OR NEW.frozen_at IS NOT NULL
       OR NEW.decided_at IS NOT NULL
       OR NEW.run_by_id IS NOT NULL
       OR NEW.run_principal_kind <> ''
       OR NEW.run_principal_id IS NOT NULL
       OR NEW.approved_by_id IS NOT NULL
       OR NEW.approved_principal_kind <> ''
       OR NEW.approved_principal_id IS NOT NULL
       OR NEW.rejected_by_id IS NOT NULL
       OR NEW.rejected_principal_kind <> ''
       OR NEW.rejected_principal_id IS NOT NULL THEN
        RAISE EXCEPTION 'new payroll periods must be clean drafts' USING ERRCODE = '23514';
    END IF;
    IF NEW.organization_timezone = '' OR NOT EXISTS (
        SELECT 1 FROM pg_timezone_names
        WHERE name = NEW.organization_timezone
    ) THEN
        RAISE EXCEPTION 'payroll organization timezone is required' USING ERRCODE = '23514';
    END IF;
    IF NEW.decision_note <> ''
       OR NEW.run_idempotency_key_hash IS NOT NULL
       OR NEW.run_fingerprint <> '' THEN
        RAISE EXCEPTION 'new payroll workflow evidence is invalid' USING ERRCODE = '23514';
    END IF;
    IF NEW.created_by_id IS NULL OR NOT payroll_principal_matches(
        NEW.created_principal_kind,
        NEW.created_principal_id,
        NEW.created_by_id
    ) THEN
        RAISE EXCEPTION 'payroll creator attribution is invalid' USING ERRCODE = '23514';
    END IF;

    IF NEW.correction_of_id IS NOT NULL THEN
        SELECT * INTO original
        FROM payroll_payrollperiod
        WHERE id = NEW.correction_of_id
        FOR UPDATE;
        IF NOT FOUND
           OR original.status <> 'rejected'
           OR original.branch_id <> NEW.branch_id
           OR original.department_id IS DISTINCT FROM NEW.department_id
           OR original.period_start <> NEW.period_start
           OR original.period_end <> NEW.period_end
           OR original.currency <> NEW.currency
           OR original.organization_timezone <> NEW.organization_timezone
           OR NEW.version <> original.version + 1
           OR NEW.correction_reason = '' THEN
            RAISE EXCEPTION 'invalid payroll correction relationship' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.version <> 1 THEN
        RAISE EXCEPTION 'initial payroll version must be one' USING ERRCODE = '23514';
    ELSIF EXISTS (
        SELECT 1
        FROM payroll_payrollperiod existing
        WHERE existing.branch_id = NEW.branch_id
          AND existing.correction_of_id IS NULL
          AND existing.period_start <= NEW.period_end
          AND existing.period_end >= NEW.period_start
          AND (
              NEW.department_id IS NULL
              OR existing.department_id IS NULL
              OR existing.department_id = NEW.department_id
          )
    ) THEN
        RAISE EXCEPTION 'overlapping payroll scope and window' USING ERRCODE = '23P01';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_period_insert_validate
BEFORE INSERT ON payroll_payrollperiod
FOR EACH ROW EXECUTE FUNCTION payroll_validate_period_insert();

CREATE OR REPLACE FUNCTION payroll_validate_line_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    period_row payroll_payrollperiod%ROWTYPE;
    teacher_row teachers_teacherprofile%ROWTYPE;
    policy_row teachers_payoutpolicy%ROWTYPE;
BEGIN
    SELECT * INTO period_row
    FROM payroll_payrollperiod
    WHERE id = NEW.period_id
    FOR UPDATE;
    IF NOT FOUND OR period_row.status <> 'draft' THEN
        RAISE EXCEPTION 'payroll lines require a draft period' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO teacher_row
    FROM teachers_teacherprofile
    WHERE id = NEW.teacher_id
    FOR UPDATE;
    IF NOT FOUND
       OR NOT teacher_row.is_active
       OR teacher_row.branch_id <> period_row.branch_id
       OR teacher_row.branch_id <> NEW.branch_at_run_id
       OR teacher_row.department_id IS DISTINCT FROM NEW.department_at_run_id
       OR (period_row.department_id IS NOT NULL
           AND period_row.department_id IS DISTINCT FROM NEW.department_at_run_id)
       OR teacher_row.user_id <> NEW.teacher_user_id_snapshot
       OR NEW.currency <> period_row.currency
       OR NEW.teacher_name_snapshot = ''
       OR NEW.teacher_code_snapshot = ''
       OR jsonb_typeof(NEW.payout_policy_snapshot) IS DISTINCT FROM 'object'
       OR jsonb_typeof(NEW.calculation_breakdown) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'payroll line scope snapshot is invalid' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO policy_row
    FROM teachers_payoutpolicy
    WHERE id = NEW.payout_policy_id_snapshot
    FOR UPDATE;
    IF NOT FOUND
       OR policy_row.teacher_id <> NEW.teacher_id
       OR NOT policy_row.is_active
       OR policy_row.method <> NEW.payout_method_snapshot
       OR NEW.payout_policy_snapshot->>'id' IS DISTINCT FROM NEW.payout_policy_id_snapshot::text
       OR NEW.payout_policy_snapshot->>'method' IS DISTINCT FROM NEW.payout_method_snapshot THEN
        RAISE EXCEPTION 'payroll line policy snapshot is invalid' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_line_insert_validate
BEFORE INSERT ON payroll_payrolllineitem
FOR EACH ROW EXECUTE FUNCTION payroll_validate_line_insert();

CREATE OR REPLACE FUNCTION payroll_validate_payslip_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    line_row payroll_payrolllineitem%ROWTYPE;
    period_row payroll_payrollperiod%ROWTYPE;
    expected_document_number text;
BEGIN
    SELECT * INTO line_row
    FROM payroll_payrolllineitem
    WHERE id = NEW.line_item_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'payroll payslip line is unavailable' USING ERRCODE = '23503';
    END IF;
    SELECT * INTO period_row
    FROM payroll_payrollperiod
    WHERE id = line_row.period_id
    FOR UPDATE;
    expected_document_number := 'PAY-'
        || lpad(period_row.id::text, GREATEST(8, length(period_row.id::text)), '0')
        || '-'
        || lpad(line_row.id::text, GREATEST(8, length(line_row.id::text)), '0');
    IF period_row.status <> 'draft'
       OR NEW.document_number IS DISTINCT FROM expected_document_number
       OR jsonb_typeof(NEW.snapshot) IS DISTINCT FROM 'object'
       OR NEW.snapshot->'period'->>'id' IS DISTINCT FROM period_row.id::text
       OR NEW.snapshot->'period'->>'label' IS DISTINCT FROM period_row.label
       OR NEW.snapshot->'period'->>'period_start' IS DISTINCT FROM period_row.period_start::text
       OR NEW.snapshot->'period'->>'period_end' IS DISTINCT FROM period_row.period_end::text
       OR NEW.snapshot->'period'->>'pay_date' IS DISTINCT FROM period_row.pay_date::text
       OR NEW.snapshot->'period'->>'organization_timezone'
          IS DISTINCT FROM period_row.organization_timezone
       OR NEW.snapshot->'teacher'->>'id' IS DISTINCT FROM line_row.teacher_id::text
       OR NEW.snapshot->'teacher'->>'code' IS DISTINCT FROM line_row.teacher_code_snapshot
       OR NEW.snapshot->'teacher'->>'name' IS DISTINCT FROM line_row.teacher_name_snapshot
       OR NEW.snapshot->>'currency' IS DISTINCT FROM line_row.currency
       OR NEW.snapshot->>'base_amount_uzs' IS DISTINCT FROM line_row.base_amount_uzs::text
       OR NEW.snapshot->>'bonus_amount_uzs' IS DISTINCT FROM line_row.bonus_amount_uzs::text
       OR NEW.snapshot->>'deduction_amount_uzs' IS DISTINCT FROM line_row.deduction_amount_uzs::text
       OR NEW.snapshot->>'net_amount_uzs' IS DISTINCT FROM line_row.net_amount_uzs::text
       OR NEW.snapshot->'calculation' IS DISTINCT FROM line_row.calculation_breakdown
       OR NEW.snapshot->'payout_policy' IS DISTINCT FROM line_row.payout_policy_snapshot THEN
        RAISE EXCEPTION 'payroll payslip snapshot does not match its immutable line'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_payslip_insert_validate
BEFORE INSERT ON payroll_payrollpayslip
FOR EACH ROW EXECUTE FUNCTION payroll_validate_payslip_insert();

CREATE OR REPLACE FUNCTION payroll_validate_adjustment_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    teacher_row teachers_teacherprofile%ROWTYPE;
BEGIN
    SELECT * INTO teacher_row
    FROM teachers_teacherprofile
    WHERE id = NEW.teacher_id
    FOR UPDATE;
    IF NOT FOUND
       OR NOT teacher_row.is_active
       OR teacher_row.branch_id <> NEW.branch_id
       OR teacher_row.department_id IS DISTINCT FROM NEW.department_id THEN
        RAISE EXCEPTION 'payroll adjustment scope snapshot is invalid' USING ERRCODE = '23514';
    END IF;
    IF NEW.state <> 'pending'
       OR NEW.applied_line_id IS NOT NULL
       OR NEW.decided_by_id IS NOT NULL
       OR NEW.decided_principal_kind <> ''
       OR NEW.decided_principal_id IS NOT NULL
       OR NEW.decided_at IS NOT NULL
       OR NEW.decision_reason <> ''
       OR NEW.created_by_id IS NULL
       OR NOT payroll_principal_matches(
           NEW.created_principal_kind,
           NEW.created_principal_id,
           NEW.created_by_id
       )
       OR NEW.idempotency_key_hash = ''
       OR NEW.operation_fingerprint = ''
       OR NEW.kind NOT IN ('bonus', 'deduction') THEN
        RAISE EXCEPTION 'new payroll adjustment evidence is invalid' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM payroll_payrollperiod period
        WHERE period.branch_id = NEW.branch_id
          AND (period.department_id IS NULL OR period.department_id IS NOT DISTINCT FROM NEW.department_id)
          AND period.period_start = NEW.effective_period_start
          AND period.period_end = NEW.effective_period_end
          AND period.status <> 'draft'
    ) THEN
        RAISE EXCEPTION 'matching payroll period is frozen' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_adjustment_insert_validate
BEFORE INSERT ON payroll_payrolladjustment
FOR EACH ROW EXECUTE FUNCTION payroll_validate_adjustment_insert();

CREATE OR REPLACE FUNCTION payroll_guard_adjustment_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    applied_period_status text;
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        -- Serialize approval with a concurrent run before inspecting periods.
        PERFORM 1 FROM teachers_teacherprofile WHERE id = OLD.teacher_id FOR UPDATE;
    END IF;
    IF ROW(
        NEW.decided_principal_kind,
        NEW.decided_principal_id,
        NEW.decided_at,
        NEW.decision_reason
    ) IS DISTINCT FROM ROW(
        OLD.decided_principal_kind,
        OLD.decided_principal_id,
        OLD.decided_at,
        OLD.decision_reason
    ) AND NOT (
        OLD.state = 'pending'
        AND NEW.state IN ('approved', 'rejected')
        AND OLD.decided_principal_id IS NULL
        AND NEW.decided_by_id IS NOT NULL
        AND payroll_principal_matches(
            NEW.decided_principal_kind,
            NEW.decided_principal_id,
            NEW.decided_by_id
        )
        AND (NEW.state <> 'rejected' OR NEW.decision_reason <> '')
    ) THEN
        RAISE EXCEPTION 'payroll adjustment decision evidence is immutable' USING ERRCODE = '55000';
    END IF;
    IF NEW.applied_line_id IS DISTINCT FROM OLD.applied_line_id THEN
        IF OLD.state = 'approved' AND NEW.state = 'applied' AND NEW.applied_line_id IS NOT NULL THEN
            IF NOT EXISTS (
                SELECT 1
                FROM payroll_payrolllineitem line
                JOIN payroll_payrollperiod period ON period.id = line.period_id
                WHERE line.id = NEW.applied_line_id
                  AND line.teacher_id = NEW.teacher_id
                  AND period.branch_id = NEW.branch_id
                  AND (period.department_id IS NULL OR period.department_id IS NOT DISTINCT FROM NEW.department_id)
                  AND period.period_start = NEW.effective_period_start
                  AND period.period_end = NEW.effective_period_end
            ) THEN
                RAISE EXCEPTION 'adjustment applied line does not match' USING ERRCODE = '23514';
            END IF;
        ELSIF OLD.state = 'applied' AND NEW.state = 'approved' AND NEW.applied_line_id IS NULL THEN
            SELECT period.status INTO applied_period_status
            FROM payroll_payrolllineitem line
            JOIN payroll_payrollperiod period ON period.id = line.period_id
            WHERE line.id = OLD.applied_line_id;
            IF applied_period_status <> 'rejected' THEN
                RAISE EXCEPTION 'only rejected payroll releases an adjustment' USING ERRCODE = '55000';
            END IF;
        ELSE
            RAISE EXCEPTION 'invalid adjustment application evidence' USING ERRCODE = '55000';
        END IF;
    END IF;
    IF OLD.state = 'pending' AND NEW.state IN ('approved', 'rejected') AND EXISTS (
        SELECT 1 FROM payroll_payrollperiod period
        WHERE period.branch_id = NEW.branch_id
          AND (period.department_id IS NULL OR period.department_id IS NOT DISTINCT FROM NEW.department_id)
          AND period.period_start = NEW.effective_period_start
          AND period.period_end = NEW.effective_period_end
          AND period.status <> 'draft'
    ) THEN
        RAISE EXCEPTION 'matching payroll period is frozen' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_adjustment_evidence_guard
BEFORE UPDATE ON payroll_payrolladjustment
FOR EACH ROW EXECUTE FUNCTION payroll_guard_adjustment_evidence();

CREATE OR REPLACE FUNCTION payroll_guard_period_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    calculated record;
BEGIN
    IF NEW.version IS DISTINCT FROM OLD.version THEN
        RAISE EXCEPTION 'payroll snapshot version is immutable' USING ERRCODE = '55000';
    END IF;
    IF ROW(
        NEW.run_principal_kind,
        NEW.run_principal_id,
        NEW.run_idempotency_key_hash,
        NEW.run_fingerprint,
        NEW.line_count,
        NEW.base_total_uzs,
        NEW.bonus_total_uzs,
        NEW.deduction_total_uzs,
        NEW.net_total_uzs,
        NEW.frozen_at
    ) IS DISTINCT FROM ROW(
        OLD.run_principal_kind,
        OLD.run_principal_id,
        OLD.run_idempotency_key_hash,
        OLD.run_fingerprint,
        OLD.line_count,
        OLD.base_total_uzs,
        OLD.bonus_total_uzs,
        OLD.deduction_total_uzs,
        OLD.net_total_uzs,
        OLD.frozen_at
    ) THEN
        IF NOT (
            OLD.status = 'draft'
            AND NEW.status = 'pending_approval'
            AND NEW.run_by_id IS NOT NULL
            AND payroll_principal_matches(
                NEW.run_principal_kind,
                NEW.run_principal_id,
                NEW.run_by_id
            )
            AND NEW.run_idempotency_key_hash IS NOT NULL
            AND NEW.run_fingerprint <> ''
            AND NEW.frozen_at IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'payroll run evidence is immutable' USING ERRCODE = '55000';
        END IF;
        SELECT
            COUNT(*)::bigint AS line_count,
            COALESCE(SUM(base_amount_uzs), 0) AS base_total,
            COALESCE(SUM(bonus_amount_uzs), 0) AS bonus_total,
            COALESCE(SUM(deduction_amount_uzs), 0) AS deduction_total,
            COALESCE(SUM(net_amount_uzs), 0) AS net_total
        INTO calculated
        FROM payroll_payrolllineitem
        WHERE period_id = NEW.id;
        IF calculated.line_count <> NEW.line_count
           OR calculated.base_total <> NEW.base_total_uzs
           OR calculated.bonus_total <> NEW.bonus_total_uzs
           OR calculated.deduction_total <> NEW.deduction_total_uzs
           OR calculated.net_total <> NEW.net_total_uzs
           OR (SELECT COUNT(*) FROM payroll_payrollpayslip payslip
               JOIN payroll_payrolllineitem line ON line.id = payslip.line_item_id
               WHERE line.period_id = NEW.id) <> NEW.line_count THEN
            RAISE EXCEPTION 'payroll frozen totals do not match immutable lines' USING ERRCODE = '23514';
        END IF;
    END IF;

    IF ROW(
        NEW.approved_principal_kind,
        NEW.approved_principal_id,
        NEW.rejected_principal_kind,
        NEW.rejected_principal_id,
        NEW.decision_note,
        NEW.decided_at
    ) IS DISTINCT FROM ROW(
        OLD.approved_principal_kind,
        OLD.approved_principal_id,
        OLD.rejected_principal_kind,
        OLD.rejected_principal_id,
        OLD.decision_note,
        OLD.decided_at
    ) THEN
        IF OLD.status <> 'pending_approval' OR NEW.decided_at IS NULL THEN
            RAISE EXCEPTION 'payroll decision evidence is immutable' USING ERRCODE = '55000';
        ELSIF NEW.status IN ('approved', 'paid') THEN
            IF NEW.approved_by_id IS NULL
               OR NOT payroll_principal_matches(
                   NEW.approved_principal_kind,
                   NEW.approved_principal_id,
                   NEW.approved_by_id
               )
               OR NEW.rejected_principal_id IS NOT NULL THEN
                RAISE EXCEPTION 'payroll approval attribution is invalid' USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.status = 'rejected' THEN
            IF NEW.rejected_by_id IS NULL
               OR NOT payroll_principal_matches(
                   NEW.rejected_principal_kind,
                   NEW.rejected_principal_id,
                   NEW.rejected_by_id
               )
               OR NEW.approved_principal_id IS NOT NULL
               OR NEW.decision_note = '' THEN
                RAISE EXCEPTION 'payroll rejection attribution is invalid' USING ERRCODE = '23514';
            END IF;
        ELSE
            RAISE EXCEPTION 'invalid payroll decision transition' USING ERRCODE = '55000';
        END IF;
    END IF;

    IF NEW.status = 'approved' AND NEW.paid_total_uzs <> 0 THEN
        RAISE EXCEPTION 'approved payroll cannot have a partial paid total' USING ERRCODE = '23514';
    ELSIF NEW.status = 'payment_in_progress' AND NOT (
        NEW.paid_total_uzs > 0 AND NEW.paid_total_uzs < NEW.net_total_uzs
    ) THEN
        RAISE EXCEPTION 'payment-in-progress total is invalid' USING ERRCODE = '23514';
    ELSIF NEW.status = 'paid' AND NEW.paid_total_uzs <> NEW.net_total_uzs THEN
        RAISE EXCEPTION 'paid payroll total is incomplete' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_period_evidence_guard
BEFORE UPDATE ON payroll_payrollperiod
FOR EACH ROW EXECUTE FUNCTION payroll_guard_period_evidence();

CREATE OR REPLACE FUNCTION payroll_validate_reconciliation_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    period_id_value bigint;
    evidence record;
    original payroll_payrollreconciliation%ROWTYPE;
    ledger approvals_ledgerentry%ROWTYPE;
    paid_total numeric;
BEGIN
    SELECT period_id INTO period_id_value
    FROM payroll_payrolllineitem
    WHERE id = NEW.line_item_id;
    IF period_id_value IS NULL THEN
        RAISE EXCEPTION 'payroll line is unavailable' USING ERRCODE = '23503';
    END IF;
    PERFORM 1 FROM payroll_payrollperiod WHERE id = period_id_value FOR UPDATE;
    SELECT
        line.net_amount_uzs,
        line.currency,
        line.branch_at_run_id,
        line.teacher_user_id_snapshot,
        period.status,
        period.created_by_id,
        period.created_principal_kind,
        period.created_principal_id,
        period.run_by_id,
        period.run_principal_kind,
        period.run_principal_id,
        period.approved_by_id,
        period.approved_principal_kind,
        period.approved_principal_id,
        period.decided_at
    INTO evidence
    FROM payroll_payrolllineitem line
    JOIN payroll_payrollperiod period ON period.id = line.period_id
    WHERE line.id = NEW.line_item_id
    FOR UPDATE OF line;
    IF NEW.recorded_by_id IS NULL OR NOT payroll_principal_matches(
        NEW.recorded_principal_kind,
        NEW.recorded_principal_id,
        NEW.recorded_by_id
    ) OR NEW.idempotency_key_hash = ''
      OR NEW.operation_fingerprint = ''
      OR NEW.external_reference = '' THEN
        RAISE EXCEPTION 'payroll reconciliation actor is invalid' USING ERRCODE = '23514';
    END IF;
    IF NEW.currency <> evidence.currency
       OR NEW.paid_at > CURRENT_TIMESTAMP + INTERVAL '5 minutes'
       OR NEW.paid_at < evidence.decided_at
       OR NEW.recorded_by_id = evidence.teacher_user_id_snapshot
       OR (NEW.recorded_principal_kind = evidence.created_principal_kind
           AND NEW.recorded_principal_id = evidence.created_principal_id)
       OR NEW.recorded_by_id = evidence.created_by_id
       OR (NEW.recorded_principal_kind = evidence.run_principal_kind
           AND NEW.recorded_principal_id = evidence.run_principal_id)
       OR NEW.recorded_by_id = evidence.run_by_id
       OR (NEW.recorded_principal_kind = evidence.approved_principal_kind
           AND NEW.recorded_principal_id = evidence.approved_principal_id)
       OR NEW.recorded_by_id = evidence.approved_by_id THEN
        RAISE EXCEPTION 'payroll reconciliation violates separation of duties' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO ledger
    FROM approvals_ledgerentry
    WHERE id = NEW.ledger_entry_id
    FOR UPDATE;
    IF NOT FOUND
       OR ledger.amount_uzs <> NEW.amount_uzs
       OR ledger.branch_id IS DISTINCT FROM evidence.branch_at_run_id
       OR ledger.payment_method_id IS DISTINCT FROM NEW.payment_method_id
       OR ledger.created_by_id IS DISTINCT FROM NEW.recorded_by_id THEN
        RAISE EXCEPTION 'payroll ledger evidence does not match reconciliation' USING ERRCODE = '23514';
    END IF;

    IF NEW.kind = 'payment' THEN
        IF NEW.reverses_id IS NOT NULL
           OR evidence.status NOT IN ('approved', 'payment_in_progress')
           OR ledger.direction <> 'out'
           OR ledger.entry_type <> 'payroll'
           OR ledger.source_kind <> 'payroll_line_item'
           OR ledger.source_id <> NEW.line_item_id THEN
            RAISE EXCEPTION 'invalid payroll payment evidence' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.kind = 'reversal' THEN
        SELECT * INTO original
        FROM payroll_payrollreconciliation
        WHERE id = NEW.reverses_id
        FOR UPDATE;
        IF NOT FOUND
           OR original.kind <> 'payment'
           OR original.line_item_id <> NEW.line_item_id
           OR original.amount_uzs <> NEW.amount_uzs
           OR original.currency <> NEW.currency
           OR original.payment_method_id <> NEW.payment_method_id
           OR NEW.paid_at < original.paid_at
           OR NEW.reason = ''
           OR evidence.status NOT IN ('approved', 'payment_in_progress', 'paid')
           OR ledger.direction <> 'in'
           OR ledger.entry_type <> 'payroll_reversal'
           OR ledger.source_kind <> 'payroll_reconciliation'
           OR ledger.source_id <> original.id THEN
            RAISE EXCEPTION 'invalid payroll reversal evidence' USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'invalid payroll reconciliation kind' USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(SUM(
        CASE WHEN kind = 'payment' THEN amount_uzs ELSE -amount_uzs END
    ), 0)
    INTO paid_total
    FROM payroll_payrollreconciliation
    WHERE line_item_id = NEW.line_item_id;
    paid_total := paid_total + CASE WHEN NEW.kind = 'payment' THEN NEW.amount_uzs ELSE -NEW.amount_uzs END;
    IF paid_total < 0 OR paid_total > evidence.net_amount_uzs THEN
        RAISE EXCEPTION 'payroll reconciliation exceeds immutable line total' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_reconciliation_insert_validate
BEFORE INSERT ON payroll_payrollreconciliation
FOR EACH ROW EXECUTE FUNCTION payroll_validate_reconciliation_insert();

CREATE OR REPLACE FUNCTION payroll_validate_period_event_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    period_status text;
BEGIN
    IF NEW.actor_id IS NULL OR NOT payroll_principal_matches(
        NEW.actor_principal_kind,
        NEW.actor_principal_id,
        NEW.actor_id
    ) THEN
        RAISE EXCEPTION 'payroll event actor is invalid' USING ERRCODE = '23514';
    END IF;
    SELECT status INTO period_status FROM payroll_payrollperiod WHERE id = NEW.period_id;
    IF NEW.idempotency_key_hash IS NULL OR NEW.operation_fingerprint = ''
       OR (NEW.action = 'run' AND period_status <> 'pending_approval')
       OR (NEW.action = 'approve' AND period_status NOT IN ('approved', 'paid'))
       OR (NEW.action = 'reject' AND period_status <> 'rejected')
       OR (NEW.action = 'payment' AND period_status NOT IN ('payment_in_progress', 'paid'))
       OR (NEW.action = 'reversal' AND period_status NOT IN ('approved', 'payment_in_progress'))
       OR NEW.action NOT IN ('run', 'approve', 'reject', 'payment', 'reversal') THEN
        RAISE EXCEPTION 'payroll period event does not match workflow state' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_period_event_insert_validate
BEFORE INSERT ON payroll_payrollperiodevent
FOR EACH ROW EXECUTE FUNCTION payroll_validate_period_event_insert();

CREATE OR REPLACE FUNCTION payroll_validate_adjustment_event_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    adjustment_state text;
BEGIN
    IF NEW.actor_id IS NULL OR NOT payroll_principal_matches(
        NEW.actor_principal_kind,
        NEW.actor_principal_id,
        NEW.actor_id
    ) THEN
        RAISE EXCEPTION 'adjustment event actor is invalid' USING ERRCODE = '23514';
    END IF;
    SELECT state INTO adjustment_state
    FROM payroll_payrolladjustment
    WHERE id = NEW.adjustment_id;
    IF (NEW.action = 'created' AND adjustment_state <> 'pending')
       OR (NEW.action = 'approved' AND adjustment_state <> 'approved')
       OR (NEW.action = 'rejected' AND adjustment_state <> 'rejected')
       OR (NEW.action = 'applied' AND adjustment_state <> 'applied')
       OR (NEW.action = 'released' AND adjustment_state <> 'approved')
       OR NEW.action NOT IN ('created', 'approved', 'rejected', 'applied', 'released') THEN
        RAISE EXCEPTION 'adjustment event does not match workflow state' USING ERRCODE = '23514';
    END IF;
    IF NEW.action IN ('approved', 'rejected') AND (
        NEW.idempotency_key_hash IS NULL OR NEW.operation_fingerprint = ''
    ) THEN
        RAISE EXCEPTION 'adjustment decision event requires an idempotency key' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_adjustment_event_insert_validate
BEFORE INSERT ON payroll_payrolladjustmentevent
FOR EACH ROW EXECUTE FUNCTION payroll_validate_adjustment_event_insert();

CREATE OR REPLACE FUNCTION payroll_validate_export_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.requested_by_id IS NULL OR NOT payroll_principal_matches(
        NEW.requested_principal_kind,
        NEW.requested_principal_id,
        NEW.requested_by_id
    ) OR NEW.status <> 'queued'
      OR NEW.idempotency_key_hash = ''
      OR NEW.operation_fingerprint = ''
      OR NEW.format NOT IN ('xlsx', 'pdf')
      OR NEW.s3_key <> ''
      OR NEW.file_bytes <> 0
      OR NEW.error_code <> ''
      OR NEW.started_at IS NOT NULL
      OR NEW.finished_at IS NOT NULL
      OR (SELECT status FROM payroll_payrollperiod WHERE id = NEW.period_id) = 'draft'
      OR jsonb_typeof(NEW.filters) <> 'object'
      OR (NEW.filters - ARRAY['teacher', 'payment_state']::text[]) <> '{}'::jsonb
      OR (NEW.filters ? 'teacher' AND NOT (
          jsonb_typeof(NEW.filters->'teacher') = 'number'
          AND (NEW.filters->>'teacher') ~ '^[1-9][0-9]*$'
      ))
      OR (NEW.filters ? 'payment_state' AND (
          jsonb_typeof(NEW.filters->'payment_state') <> 'string'
          OR NEW.filters->>'payment_state' NOT IN ('unpaid', 'partial', 'paid')
      )) THEN
        RAISE EXCEPTION 'invalid payroll export request evidence' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_export_insert_validate
BEFORE INSERT ON payroll_payrollexport
FOR EACH ROW EXECUTE FUNCTION payroll_validate_export_insert();

CREATE OR REPLACE FUNCTION payroll_guard_export_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT (
        NEW.status = OLD.status
        OR (OLD.status = 'queued' AND NEW.status IN ('running', 'failed'))
        OR (OLD.status = 'running' AND NEW.status IN ('queued', 'done', 'failed'))
    ) THEN
        RAISE EXCEPTION 'invalid payroll export transition' USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'queued' AND NOT (
        NEW.s3_key = '' AND NEW.file_bytes = 0 AND NEW.error_code = ''
        AND NEW.started_at IS NULL AND NEW.finished_at IS NULL
    ) THEN
        RAISE EXCEPTION 'queued payroll export evidence is invalid' USING ERRCODE = '23514';
    ELSIF NEW.status = 'running' AND NOT (
        NEW.s3_key = '' AND NEW.file_bytes = 0 AND NEW.error_code = ''
        AND NEW.started_at IS NOT NULL AND NEW.finished_at IS NULL
    ) THEN
        RAISE EXCEPTION 'running payroll export evidence is invalid' USING ERRCODE = '23514';
    ELSIF NEW.status = 'done' AND NOT (
        NEW.s3_key <> '' AND NEW.file_bytes > 0 AND NEW.error_code = ''
        AND NEW.started_at IS NOT NULL AND NEW.finished_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'completed payroll export evidence is invalid' USING ERRCODE = '23514';
    ELSIF NEW.status = 'failed' AND NOT (
        NEW.s3_key = '' AND NEW.file_bytes = 0 AND NEW.error_code <> ''
        AND NEW.finished_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'failed payroll export evidence is invalid' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payroll_export_state_guard
BEFORE UPDATE ON payroll_payrollexport
FOR EACH ROW EXECUTE FUNCTION payroll_guard_export_state();

CREATE OR REPLACE FUNCTION payroll_check_period_paid_total(target_period_id bigint)
RETURNS void
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
DECLARE
    stored_total numeric;
    reconciliation_total numeric;
BEGIN
    IF target_period_id IS NULL THEN
        RETURN;
    END IF;
    SELECT paid_total_uzs INTO stored_total
    FROM payroll_payrollperiod
    WHERE id = target_period_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    SELECT COALESCE(SUM(
        CASE WHEN reconciliation.kind = 'payment'
             THEN reconciliation.amount_uzs
             ELSE -reconciliation.amount_uzs END
    ), 0)
    INTO reconciliation_total
    FROM payroll_payrollreconciliation reconciliation
    JOIN payroll_payrolllineitem line ON line.id = reconciliation.line_item_id
    WHERE line.period_id = target_period_id;
    IF stored_total <> reconciliation_total THEN
        RAISE EXCEPTION 'payroll paid total does not match reconciliation evidence'
            USING ERRCODE = '23514';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION payroll_assert_period_row_paid_total()
RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
BEGIN
    PERFORM payroll_check_period_paid_total(NEW.id);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION payroll_assert_reconciliation_paid_total()
RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
DECLARE
    target_period_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT period_id INTO target_period_id
        FROM payroll_payrolllineitem
        WHERE id = OLD.line_item_id;
    ELSE
        SELECT period_id INTO target_period_id
        FROM payroll_payrolllineitem
        WHERE id = NEW.line_item_id;
    END IF;
    PERFORM payroll_check_period_paid_total(target_period_id);
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER payroll_period_paid_total_check
AFTER INSERT OR UPDATE ON payroll_payrollperiod
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION payroll_assert_period_row_paid_total();

CREATE CONSTRAINT TRIGGER payroll_reconciliation_paid_total_check
AFTER INSERT OR UPDATE OR DELETE ON payroll_payrollreconciliation
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION payroll_assert_reconciliation_paid_total();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS payroll_reconciliation_paid_total_check ON payroll_payrollreconciliation;
DROP TRIGGER IF EXISTS payroll_period_paid_total_check ON payroll_payrollperiod;
DROP TRIGGER IF EXISTS payroll_export_state_guard ON payroll_payrollexport;
DROP TRIGGER IF EXISTS payroll_export_insert_validate ON payroll_payrollexport;
DROP TRIGGER IF EXISTS payroll_adjustment_event_insert_validate ON payroll_payrolladjustmentevent;
DROP TRIGGER IF EXISTS payroll_period_event_insert_validate ON payroll_payrollperiodevent;
DROP TRIGGER IF EXISTS payroll_reconciliation_insert_validate ON payroll_payrollreconciliation;
DROP TRIGGER IF EXISTS payroll_period_evidence_guard ON payroll_payrollperiod;
DROP TRIGGER IF EXISTS payroll_adjustment_evidence_guard ON payroll_payrolladjustment;
DROP TRIGGER IF EXISTS payroll_adjustment_insert_validate ON payroll_payrolladjustment;
DROP TRIGGER IF EXISTS payroll_line_insert_validate ON payroll_payrolllineitem;
DROP TRIGGER IF EXISTS payroll_payslip_insert_validate ON payroll_payrollpayslip;
DROP TRIGGER IF EXISTS payroll_period_insert_validate ON payroll_payrollperiod;
DROP FUNCTION IF EXISTS payroll_assert_reconciliation_paid_total();
DROP FUNCTION IF EXISTS payroll_assert_period_row_paid_total();
DROP FUNCTION IF EXISTS payroll_check_period_paid_total(bigint);
DROP FUNCTION IF EXISTS payroll_guard_export_state();
DROP FUNCTION IF EXISTS payroll_validate_export_insert();
DROP FUNCTION IF EXISTS payroll_validate_adjustment_event_insert();
DROP FUNCTION IF EXISTS payroll_validate_period_event_insert();
DROP FUNCTION IF EXISTS payroll_validate_reconciliation_insert();
DROP FUNCTION IF EXISTS payroll_guard_period_evidence();
DROP FUNCTION IF EXISTS payroll_guard_adjustment_evidence();
DROP FUNCTION IF EXISTS payroll_validate_adjustment_insert();
DROP FUNCTION IF EXISTS payroll_validate_line_insert();
DROP FUNCTION IF EXISTS payroll_validate_payslip_insert();
DROP FUNCTION IF EXISTS payroll_validate_period_insert();
DROP FUNCTION IF EXISTS payroll_principal_matches(text, bigint, bigint);
"""


class Migration(migrations.Migration):
    dependencies = [("payroll", "0002_immutability")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
