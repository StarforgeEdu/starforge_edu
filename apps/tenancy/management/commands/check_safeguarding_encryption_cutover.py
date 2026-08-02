"""Detect tenant schemas that still require a non-rolling maintenance cutover."""

from __future__ import annotations

from collections.abc import Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

REQUIRED_MIGRATIONS = frozenset(
    {
        # This release changes database-enforced authorization, append-only
        # history, immutable scope, and exact role-principal attribution across
        # many domains.  Treat the complete change-set as one non-rolling
        # boundary: an old API or worker must never create legacy-shaped rows
        # after any tenant has crossed it.
        ("academics", "0004_assessment_integrity"),
        ("access", "0003_registrar_safeguarding_permissions"),
        ("access", "0004_compensation_permissions"),
        ("access", "0005_protect_owner_authority"),
        ("access", "0006_head_of_department_org_read"),
        # Captures immutable role-principal/scope authority, encrypts retained
        # output, and refuses in-flight requests. Old AI workers must be fully
        # drained before this migration can run.
        ("ai_app", "0015_ai_request_scope_privacy"),
        ("approvals", "0004_approval_idempotency"),
        ("assignments", "0004_uploadgrant_source_cleanup"),
        ("audit", "0005_audit_scope_snapshot"),
        ("finance", "0009_invoice_historical_scope"),
        ("forms_app", "0003_role_principal_attribution"),
        ("meetings", "0002_attendee_principal_attribution"),
        ("messaging", "0005_uploadgrant_source_cleanup"),
        ("messaging", "0006_threadparticipant_principal_attribution"),
        ("parents", "0009_encrypt_safeguarding_text"),
        ("parents", "0008_parent_creation_scope"),
        ("parents", "0010_preserve_family_lifecycle_history"),
        ("payments", "0005_payment_historical_scope"),
        ("payments", "0006_fiscalreceipt_trusted_fields"),
        # Removes legacy raw webhook/attempt columns. Old web and worker images
        # still reference those columns and therefore must be drained first.
        ("payments", "0007_webhook_privacy_and_txn_integrity"),
        ("payments", "0008_external_provider_transaction_integrity"),
        # Quarantines every pre-lease physical-print attempt and changes the
        # device wire contract. Old agents/workers must be stopped before this
        # schema is exposed; otherwise they can create unleased in-flight rows.
        ("printing", "0005_print_job_delivery_lease"),
        # Replaces bridge-user notification ownership with an exact immutable
        # role principal. Old workers can call external providers before the new
        # delivery trigger runs, so they must never share this migrated schema.
        ("notifications", "0012_recipient_principal_attribution"),
        ("org", "0019_centersettings_organization_timezone"),
        ("org", "0020_org_scope_and_history_integrity"),
        ("org", "0021_durable_center_settings"),
        ("reports", "0006_report_scope_params_indexes"),
        ("students", "0010_encrypt_emergency_contacts"),
        ("students", "0011_protect_identity_history"),
        ("staff_tasks", "0003_task_assignee_principal"),
        ("teachers", "0009_protect_payout_policy_history"),
        ("teachers", "0010_alter_payoutpolicy_method"),
    }
)

# Audit is intentionally installed in both shared/public and tenant schemas.
# The public admin/API process is stopped for the same boundary, and its
# append-only scope migration must be proven separately rather than being
# accidentally treated as a tenant-only concern.
REQUIRED_SHARED_MIGRATIONS = frozenset({("audit", "0005_audit_scope_snapshot")})

# A brand-new schema has no preceding application process or legacy row to
# drain.  These immediate predecessors distinguish that safe bootstrap from an
# established or partially upgraded tenant.  Once any release migration itself
# is recorded, the boundary also remains active until the whole set is clear.
LEGACY_TENANT_ANCHORS = frozenset(
    {
        ("academics", "0003_examtype_remove_exam_type_exam_exam_type"),
        ("access", "0002_accounttype_accounttypepermission"),
        ("ai_app", "0014_seed_template_generation_prompt"),
        ("approvals", "0003_ledgerentry_database_immutability"),
        ("assignments", "0003_assignmentuploadgrant"),
        ("audit", "0004_auditlog_database_immutability"),
        ("finance", "0008_installment_amount_positive"),
        ("forms_app", "0002_form_audience_roles_form_audience_user_ids"),
        ("meetings", "0001_initial"),
        ("messaging", "0004_threadparticipant_notifications_muted"),
        ("notifications", "0011_alter_notificationdelivery_status"),
        ("org", "0018_staffprofile_staff_phone_unique_nonblank_and_more"),
        ("parents", "0007_parentprofile_parent_phone_unique_nonblank_and_more"),
        ("payments", "0004_payment_account_ref_index"),
        ("printing", "0004_printjob_unique_open_source"),
        ("reports", "0005_scope_hod_aggregate_reports"),
        ("students", "0009_studentprofile_student_phone_unique_nonblank_and_more"),
        ("staff_tasks", "0002_task_task_created_idx"),
        ("teachers", "0008_teachertype"),
    }
)


def requires_safeguarding_cutover(applied: Iterable[tuple[str, str]]) -> bool:
    """Return whether any maintenance-only tenant migration is not recorded."""
    applied_set = set(applied)
    established = bool(applied_set & (LEGACY_TENANT_ANCHORS | REQUIRED_MIGRATIONS))
    return established and not REQUIRED_MIGRATIONS.issubset(applied_set)


def pending_tenant_schema_count() -> tuple[int, int]:
    """Return ``(pending, inspected)`` without reading any tenant business row."""
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
                "SELECT table_schema FROM information_schema.tables WHERE table_name = 'django_migrations'"
            )
            migration_table_schemas = {row[0] for row in cursor.fetchall()}

    missing = sorted(set(schema_names) - migration_table_schemas)
    if missing:
        # Do not fall through to public.django_migrations via PostgreSQL's
        # search path; that would falsely classify an absent tenant schema as
        # already migrated. Counts are sufficient evidence and avoid emitting
        # tenant identifiers into deployment logs.
        raise CommandError(f"Cannot inspect {len(missing)} tenant schema(s) without a local migration table.")

    pending = 0
    for schema_name in schema_names:
        with schema_context(schema_name):
            applied = MigrationRecorder(connection).applied_migrations()
        if requires_safeguarding_cutover(applied):
            pending += 1
    return pending, len(schema_names)


def shared_cutover_required() -> bool:
    public_schema = get_public_schema_name()
    with schema_context(public_schema):
        applied = MigrationRecorder(connection).applied_migrations()
    established = ("audit", "0004_auditlog_database_immutability") in applied
    return established and not REQUIRED_SHARED_MIGRATIONS.issubset(applied)


class Command(BaseCommand):
    help = "Report whether any non-rolling release migration is pending in a tenant schema."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--token",
            action="store_true",
            help="Print only 'required' or 'clear' for deployment automation.",
        )

    def handle(self, *args, **options):
        pending, inspected = pending_tenant_schema_count()
        shared_required = shared_cutover_required()
        required = pending > 0 or shared_required
        if options["token"]:
            self.stdout.write("required" if required else "clear")
            return
        if required:
            self.stdout.write(
                self.style.WARNING(
                    f"Maintenance cutover required for {pending} of {inspected} tenant schemas; "
                    f"shared_required={str(shared_required).lower()}."
                )
            )
            return
        self.stdout.write(self.style.SUCCESS(f"Cutover clear across {inspected} tenant schemas."))
