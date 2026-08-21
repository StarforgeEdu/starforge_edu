from django.db import migrations


FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION ledger_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.ledger_maintenance', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    -- Preserve the entry when its actor account is deleted. This is the sole
    -- content-neutral FK maintenance update allowed by the schema.
    IF TG_OP = 'UPDATE'
       AND OLD.created_by_id IS NOT NULL
       AND NEW.created_by_id IS NULL
       AND (to_jsonb(NEW) - 'created_by_id') = (to_jsonb(OLD) - 'created_by_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'ledger entries are append-only'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS ledger_entry_immutable ON approvals_ledgerentry;
CREATE TRIGGER ledger_entry_immutable
BEFORE UPDATE OR DELETE ON approvals_ledgerentry
FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation();
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS ledger_entry_immutable ON approvals_ledgerentry;
DROP FUNCTION IF EXISTS ledger_reject_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("approvals", "0002_approvalrequest_apprreq_created_idx_and_more")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
