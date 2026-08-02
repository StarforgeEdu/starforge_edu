import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.finance.models

STATEMENT_EXPORT_INTEGRITY_SQL = r"""
CREATE OR REPLACE FUNCTION finance_statement_export_identity_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.student_id IS DISTINCT FROM OLD.student_id
       OR NEW.requested_by_id_snapshot IS DISTINCT FROM OLD.requested_by_id_snapshot
       OR NEW.requested_principal_kind IS DISTINCT FROM OLD.requested_principal_kind
       OR NEW.requested_principal_id IS DISTINCT FROM OLD.requested_principal_id
       OR NEW.locale IS DISTINCT FROM OLD.locale
       OR NEW.invoice_set_hash IS DISTINCT FROM OLD.invoice_set_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'statement export request identity is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_statement_export_invoice_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    export_student_id bigint;
    export_status varchar;
    invoice_student_id bigint;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'statement export invoice snapshot is immutable' USING ERRCODE = '55000';
    END IF;
    SELECT student_id, status INTO export_student_id, export_status
      FROM finance_statementexport WHERE id = NEW.export_id;
    SELECT student_id INTO invoice_student_id
      FROM finance_invoice WHERE id = NEW.invoice_id;
    IF export_status IS DISTINCT FROM 'queued'
       OR export_student_id IS DISTINCT FROM invoice_student_id THEN
        RAISE EXCEPTION 'statement export invoice snapshot is invalid' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER finance_statement_export_identity_immutable
BEFORE UPDATE ON finance_statementexport
FOR EACH ROW EXECUTE FUNCTION finance_statement_export_identity_guard();

CREATE TRIGGER finance_statement_export_invoice_insert_valid
BEFORE INSERT ON finance_statementexportinvoice
FOR EACH ROW EXECUTE FUNCTION finance_statement_export_invoice_guard();

CREATE TRIGGER finance_statement_export_invoice_update_immutable
BEFORE UPDATE ON finance_statementexportinvoice
FOR EACH ROW EXECUTE FUNCTION finance_statement_export_invoice_guard();

CREATE TRIGGER finance_statement_export_invoice_delete_immutable
BEFORE DELETE ON finance_statementexportinvoice
FOR EACH ROW EXECUTE FUNCTION finance_statement_export_invoice_guard();
"""

STATEMENT_EXPORT_INTEGRITY_REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS finance_statement_export_invoice_delete_immutable ON finance_statementexportinvoice;
DROP TRIGGER IF EXISTS finance_statement_export_invoice_update_immutable ON finance_statementexportinvoice;
DROP TRIGGER IF EXISTS finance_statement_export_invoice_insert_valid ON finance_statementexportinvoice;
DROP TRIGGER IF EXISTS finance_statement_export_identity_immutable ON finance_statementexport;
DROP FUNCTION IF EXISTS finance_statement_export_invoice_guard();
DROP FUNCTION IF EXISTS finance_statement_export_identity_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0010_invoice_currency_uzs"),
        ("students", "0011_protect_identity_history"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StatementExport",
            fields=[
                (
                    "id",
                    models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
                ),
                ("requested_by_id_snapshot", models.PositiveBigIntegerField()),
                (
                    "requested_principal_kind",
                    models.CharField(choices=[("staff", "Staff"), ("teacher", "Teacher")], max_length=16),
                ),
                ("requested_principal_id", models.PositiveBigIntegerField()),
                (
                    "locale",
                    models.CharField(
                        choices=[("en", "English"), ("ru", "Russian"), ("uz", "Uzbek")],
                        default="en",
                        max_length=2,
                    ),
                ),
                ("invoice_set_hash", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                            ("expired", "Expired"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("file_bytes", models.PositiveBigIntegerField(default=0)),
                ("error_code", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "expires_at",
                    models.DateTimeField(db_index=True, default=apps.finance.models.statement_export_expiry),
                ),
                ("artifact_deleted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="statement_exports",
                        to="students.studentprofile",
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.CreateModel(
            name="StatementExportInvoice",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "export",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invoice_links",
                        to="finance.statementexport",
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="statement_export_links",
                        to="finance.invoice",
                    ),
                ),
            ],
            options={
                "ordering": ("invoice_id",),
            },
        ),
        migrations.AddIndex(
            model_name="statementexport",
            index=models.Index(
                fields=["requested_by_id_snapshot", "status", "created_at"],
                name="stmt_export_owner_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="statementexport",
            index=models.Index(
                fields=["student", "invoice_set_hash", "created_at"], name="stmt_export_student_set_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="statementexport",
            index=models.Index(fields=["status", "expires_at"], name="stmt_export_status_exp_idx"),
        ),
        migrations.AddConstraint(
            model_name="statementexport",
            constraint=models.CheckConstraint(
                condition=models.Q(("expires_at__gt", models.F("created_at"))),
                name="stmt_export_expiry_after_create",
            ),
        ),
        migrations.AddConstraint(
            model_name="statementexport",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("artifact_deleted_at__isnull", True),
                        ("error_code", ""),
                        ("file_bytes", 0),
                        ("finished_at__isnull", True),
                        ("started_at__isnull", True),
                        ("status", "queued"),
                    ),
                    models.Q(
                        ("artifact_deleted_at__isnull", True),
                        ("attempt_count__gt", 0),
                        ("error_code", ""),
                        ("file_bytes", 0),
                        ("finished_at__isnull", True),
                        ("started_at__isnull", False),
                        ("status", "running"),
                    ),
                    models.Q(
                        ("artifact_deleted_at__isnull", True),
                        ("attempt_count__gt", 0),
                        ("error_code", ""),
                        ("file_bytes__gt", 0),
                        ("finished_at__isnull", False),
                        ("started_at__isnull", False),
                        ("status", "done"),
                    ),
                    models.Q(
                        ("artifact_deleted_at__isnull", True),
                        ("attempt_count__gt", 0),
                        ("error_code", "statement_generation_failed"),
                        ("file_bytes", 0),
                        ("finished_at__isnull", False),
                        ("started_at__isnull", False),
                        ("status", "failed"),
                    ),
                    models.Q(("finished_at__isnull", False), ("status", "expired")),
                    _connector="OR",
                ),
                name="stmt_export_status_shape",
            ),
        ),
        migrations.AddIndex(
            model_name="statementexportinvoice",
            index=models.Index(fields=["invoice", "export"], name="stmt_export_invoice_lookup_idx"),
        ),
        migrations.AddConstraint(
            model_name="statementexportinvoice",
            constraint=models.UniqueConstraint(
                fields=("export", "invoice"), name="stmt_export_invoice_unique"
            ),
        ),
        migrations.RunSQL(
            sql=STATEMENT_EXPORT_INTEGRITY_SQL,
            reverse_sql=STATEMENT_EXPORT_INTEGRITY_REVERSE_SQL,
        ),
    ]
