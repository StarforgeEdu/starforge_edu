from django.db import migrations


def ensure_public_audit_table(apps, schema_editor):
    """Backfill public installs where tenant routing marked 0001-0003 applied.

    django-tenants keeps one migration history in each schema. Before Audit was
    promoted to SHARED_APPS, public migrations were recorded while their model
    operations were skipped, so merely changing the router cannot replay 0002.
    """
    table = "audit_auditlog"
    with schema_editor.connection.cursor() as cursor:
        tables = schema_editor.connection.introspection.table_names(cursor)
    if table not in tables:
        schema_editor.create_model(apps.get_model("audit", "AuditLog"))

FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION audit_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('starforge.audit_maintenance', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    -- AuditLog.actor uses SET NULL so deleting a user preserves history. Permit
    -- exactly that FK-maintenance change while rejecting every content edit.
    IF TG_OP = 'UPDATE'
       AND OLD.actor_id IS NOT NULL
       AND NEW.actor_id IS NULL
       AND (to_jsonb(NEW) - 'actor_id') = (to_jsonb(OLD) - 'actor_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'audit logs are append-only'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS audit_log_immutable ON audit_auditlog;
CREATE TRIGGER audit_log_immutable
BEFORE UPDATE OR DELETE ON audit_auditlog
FOR EACH ROW EXECUTE FUNCTION audit_reject_mutation();
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS audit_log_immutable ON audit_auditlog;
DROP FUNCTION IF EXISTS audit_reject_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0003_alter_auditlog_action")]

    operations = [
        migrations.RunPython(ensure_public_audit_table, migrations.RunPython.noop),
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
