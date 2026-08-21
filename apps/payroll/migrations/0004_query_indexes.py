from django.db import migrations


LESSON_INDEX_SQL = r"""
CREATE INDEX IF NOT EXISTS payroll_lesson_completed_teacher_time_idx
ON schedule_lesson (teacher_id, starts_at, cohort_id)
WHERE status = 'completed'
"""
DROP_LESSON_INDEX_SQL = "DROP INDEX IF EXISTS payroll_lesson_completed_teacher_time_idx"

ALLOCATION_INDEX_SQL = r"""
CREATE INDEX IF NOT EXISTS payroll_alloc_invoice_time_idx
ON finance_paymentallocation (invoice_id, created_at)
"""
DROP_ALLOCATION_INDEX_SQL = "DROP INDEX IF EXISTS payroll_alloc_invoice_time_idx"


class Migration(migrations.Migration):
    # Tenant provisioning runs the tenant migration graph inside the same
    # transaction that creates the tenant.  Concurrent index DDL would make a
    # fresh tenant impossible to provision, so these indexes intentionally use
    # transaction-compatible, idempotent PostgreSQL DDL.

    dependencies = [
        ("payroll", "0003_relational_invariants"),
        ("schedule", "0005_single_current_term"),
    ]

    operations = [
        migrations.RunSQL(LESSON_INDEX_SQL, DROP_LESSON_INDEX_SQL),
        migrations.RunSQL(ALLOCATION_INDEX_SQL, DROP_ALLOCATION_INDEX_SQL),
    ]
