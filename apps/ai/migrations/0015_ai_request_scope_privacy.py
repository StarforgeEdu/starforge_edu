"""Immutable AI attribution, encrypted output, and bounded content retention.

Legacy bridge-user rows cannot be safely assigned to a role-native principal or
historical branch after the fact.  They are deliberately quarantined as
unresolved; no migration guesses authority from current placement.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone

import apps.ai.models
import core.fields

logger = logging.getLogger("starforge.migrations")


CREATE_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION ai_guard_prompt_version()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.feature IS DISTINCT FROM NEW.feature
       OR OLD.version IS DISTINCT FROM NEW.version
       OR OLD.system_prompt IS DISTINCT FROM NEW.system_prompt
       OR OLD.user_template IS DISTINCT FROM NEW.user_template
       OR OLD.max_output_tokens IS DISTINCT FROM NEW.max_output_tokens
       OR OLD.effort IS DISTINCT FROM NEW.effort
       OR OLD.token_cost_cap IS DISTINCT FROM NEW.token_cost_cap THEN
        RAISE EXCEPTION 'AI prompt versions are immutable; create a new version'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ai_prompt_version_immutable ON ai_app_aiprompt;
CREATE TRIGGER ai_prompt_version_immutable
BEFORE UPDATE ON ai_app_aiprompt
FOR EACH ROW EXECUTE FUNCTION ai_guard_prompt_version();

CREATE OR REPLACE FUNCTION ai_guard_request_attribution()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    principal_is_live boolean := false;
    department_branch_id bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND (
       OLD.feature IS DISTINCT FROM NEW.feature
       OR OLD.prompt_id IS DISTINCT FROM NEW.prompt_id
       OR OLD.requested_by_id IS DISTINCT FROM NEW.requested_by_id
       OR OLD.requested_principal_kind IS DISTINCT FROM NEW.requested_principal_kind
       OR OLD.requested_principal_id IS DISTINCT FROM NEW.requested_principal_id
       OR OLD.attribution_status IS DISTINCT FROM NEW.attribution_status
       OR OLD.scope_status IS DISTINCT FROM NEW.scope_status
       OR OLD.branch_at_request_id IS DISTINCT FROM NEW.branch_at_request_id
       OR OLD.department_at_request_id IS DISTINCT FROM NEW.department_at_request_id
       OR OLD.authorization_permission IS DISTINCT FROM NEW.authorization_permission
       OR OLD.source_app IS DISTINCT FROM NEW.source_app
       OR OLD.source_id IS DISTINCT FROM NEW.source_id
       OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
       OR OLD.parameter_fingerprint IS DISTINCT FROM NEW.parameter_fingerprint
       OR OLD.content_expires_at IS DISTINCT FROM NEW.content_expires_at
    ) THEN
        RAISE EXCEPTION 'AI request identity, scope, and attribution are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.provider_attempt_id <> ''
       AND OLD.provider_attempt_id IS DISTINCT FROM NEW.provider_attempt_id THEN
        RAISE EXCEPTION 'AI provider attempt identity is immutable once started'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.provider_attempted_at IS NOT NULL
       AND OLD.provider_attempted_at IS DISTINCT FROM NEW.provider_attempted_at THEN
        RAISE EXCEPTION 'AI provider attempt timestamp is immutable once started'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.provider_request_id <> ''
       AND OLD.provider_request_id IS DISTINCT FROM NEW.provider_request_id THEN
        RAISE EXCEPTION 'AI provider receipt is immutable once captured'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.provider_stop_reason <> ''
       AND OLD.provider_stop_reason IS DISTINCT FROM NEW.provider_stop_reason THEN
        RAISE EXCEPTION 'AI provider stop reason is immutable once captured'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.provider_reconciliation_status <> ''
       AND (
           OLD.provider_reconciliation_status IS DISTINCT FROM NEW.provider_reconciliation_status
           OR OLD.provider_reconciliation_reference IS DISTINCT FROM NEW.provider_reconciliation_reference
           OR OLD.provider_reconciled_at IS DISTINCT FROM NEW.provider_reconciled_at
       ) THEN
        RAISE EXCEPTION 'AI provider reconciliation evidence is immutable once captured'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NOT (
           OLD.status = 'uncertain'
           AND OLD.provider_request_id = ''
           AND OLD.provider_reconciliation_status = ''
           AND NEW.provider_reconciliation_status = 'charged'
           AND NEW.provider_request_id <> ''
       )
       AND (
           OLD.status IN ('succeeded', 'failed', 'denied_budget')
           OR NEW.status IN ('succeeded', 'failed', 'denied_budget')
           OR OLD.provider_request_id <> ''
           OR OLD.provider_reconciliation_status <> ''
       )
       AND (
           OLD.input_tokens IS DISTINCT FROM NEW.input_tokens
           OR OLD.output_tokens IS DISTINCT FROM NEW.output_tokens
           OR OLD.cache_read_tokens IS DISTINCT FROM NEW.cache_read_tokens
           OR OLD.cache_creation_tokens IS DISTINCT FROM NEW.cache_creation_tokens
           OR OLD.cost_microusd IS DISTINCT FROM NEW.cost_microusd
       ) THEN
        RAISE EXCEPTION 'Final AI provider accounting is immutable'
            USING ERRCODE = '55000';
    END IF;

    -- Validate ownership only at capture. Later controlled lifecycle updates
    -- remain possible after an account is deactivated; workers independently
    -- re-authorize immediately before any provider call or output application.
    IF TG_OP = 'INSERT' AND NEW.attribution_status = 'resolved' THEN
        IF NEW.authorization_permission = ''
           OR NEW.scope_status NOT IN ('organization', 'resolved') THEN
            RAISE EXCEPTION 'resolved AI request requires permission and resolved scope'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.requested_principal_kind = 'student' THEN
            SELECT EXISTS (
                SELECT 1 FROM students_studentprofile profile
                JOIN users_user bridge ON bridge.id = profile.user_id
                WHERE profile.id = NEW.requested_principal_id
                  AND profile.user_id = NEW.requested_by_id
                  AND profile.is_active AND bridge.is_active
            ) INTO principal_is_live;
        ELSIF NEW.requested_principal_kind = 'teacher' THEN
            SELECT EXISTS (
                SELECT 1 FROM teachers_teacherprofile profile
                JOIN users_user bridge ON bridge.id = profile.user_id
                WHERE profile.id = NEW.requested_principal_id
                  AND profile.user_id = NEW.requested_by_id
                  AND profile.is_active AND bridge.is_active
            ) INTO principal_is_live;
        ELSIF NEW.requested_principal_kind = 'parent' THEN
            SELECT EXISTS (
                SELECT 1 FROM parents_parentprofile profile
                JOIN users_user bridge ON bridge.id = profile.user_id
                WHERE profile.id = NEW.requested_principal_id
                  AND profile.user_id = NEW.requested_by_id
                  AND profile.is_active AND bridge.is_active
            ) INTO principal_is_live;
        ELSIF NEW.requested_principal_kind = 'staff' THEN
            SELECT EXISTS (
                SELECT 1 FROM org_staffprofile profile
                JOIN users_user bridge ON bridge.id = profile.user_id
                WHERE profile.id = NEW.requested_principal_id
                  AND profile.user_id = NEW.requested_by_id
                  AND profile.is_active AND bridge.is_active
            ) INTO principal_is_live;
        END IF;
        IF NOT principal_is_live THEN
            RAISE EXCEPTION 'AI requester principal is not active or owned by user'
                USING ERRCODE = '23514';
        END IF;
    ELSIF TG_OP = 'INSERT' AND NEW.attribution_status = 'unresolved' AND (
        NEW.authorization_permission <> ''
        OR NEW.scope_status <> 'unresolved'
        OR NEW.requested_principal_kind <> ''
        OR NEW.requested_principal_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'unresolved AI request cannot carry authority'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'INSERT' AND NEW.department_at_request_id IS NOT NULL THEN
        SELECT branch_id INTO department_branch_id
        FROM org_department
        WHERE id = NEW.department_at_request_id;
        IF department_branch_id IS NULL
           OR department_branch_id IS DISTINCT FROM NEW.branch_at_request_id THEN
            RAISE EXCEPTION 'AI request department does not belong to captured branch'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ai_request_identity_immutable ON ai_app_airequest;
CREATE TRIGGER ai_request_identity_immutable
BEFORE INSERT OR UPDATE ON ai_app_airequest
FOR EACH ROW EXECUTE FUNCTION ai_guard_request_attribution();

CREATE OR REPLACE FUNCTION ai_reject_request_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'AI accounting evidence is append-only; purge sensitive content instead'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS ai_request_reject_delete ON ai_app_airequest;
CREATE TRIGGER ai_request_reject_delete
BEFORE DELETE ON ai_app_airequest
FOR EACH ROW EXECUTE FUNCTION ai_reject_request_delete();
"""


DROP_GUARDS_SQL = r"""
DROP TRIGGER IF EXISTS ai_prompt_version_immutable ON ai_app_aiprompt;
DROP TRIGGER IF EXISTS ai_request_identity_immutable ON ai_app_airequest;
DROP TRIGGER IF EXISTS ai_request_reject_delete ON ai_app_airequest;
DROP FUNCTION IF EXISTS ai_guard_prompt_version();
DROP FUNCTION IF EXISTS ai_guard_request_attribution();
DROP FUNCTION IF EXISTS ai_reject_request_delete();
"""


def protect_legacy_content(apps, schema_editor):
    AIPrompt = apps.get_model("ai_app", "AIPrompt")
    AIRequest = apps.get_model("ai_app", "AIRequest")
    TenantAIBudget = apps.get_model("ai_app", "TenantAIBudget")
    alias = schema_editor.connection.alias
    now = timezone.now()
    retention = timedelta(days=30)
    migrated = 0
    purged = 0
    invalid_cost = AIRequest.objects.using(alias).filter(cost_microusd__lt=0).count()
    if invalid_cost:
        raise RuntimeError(
            f"AI migration refused {invalid_cost} requests with negative cost; review them explicitly."
        )
    invalid_budget = (
        TenantAIBudget.objects.using(alias)
        .filter(monthly_token_limit__lt=models.F("daily_token_limit"))
        .count()
    )
    if invalid_budget:
        raise RuntimeError("AI migration refused a budget whose monthly limit is below its daily limit.")
    invalid_prompts = (
        AIPrompt.objects.using(alias)
        .filter(
            ~models.Q(effort__in=("low", "medium", "high", "max"))
            | models.Q(max_output_tokens=0)
            | models.Q(token_cost_cap__lt=models.F("max_output_tokens"))
        )
        .count()
    )
    if invalid_prompts:
        raise RuntimeError(
            f"AI migration refused {invalid_prompts} invalid prompt versions; review them explicitly."
        )
    nonterminal = AIRequest.objects.using(alias).filter(status__in=("queued", "running")).count()
    if nonterminal:
        raise RuntimeError(f"AI migration refused {nonterminal} in-flight requests; drain workers and retry.")
    stranded_reservations = (
        AIRequest.objects.using(alias)
        .filter(
            status__in=("succeeded", "failed", "denied_budget"),
            reserved_tokens__gt=0,
        )
        .count()
    )
    if stranded_reservations:
        raise RuntimeError(
            "AI migration refused "
            f"{stranded_reservations} terminal requests with reserved tokens; "
            "reconcile their tenant budgets explicitly."
        )

    # Older workers stored raw exception strings here. They can contain source
    # text, URLs, provider response fragments, or credentials; retain only the
    # fact that a historical failure occurred before narrowing the column.
    AIRequest.objects.using(alias).exclude(error_detail="").update(error_detail="legacy_failure")

    terminal = {"succeeded", "failed", "denied_budget"}
    last_pk = 0
    while True:
        rows = list(
            AIRequest.objects.using(alias)
            .filter(pk__gt=last_pk)
            .order_by("pk")
            .values("pk", "created_at", "status", "output_text")[:500]
        )
        if not rows:
            break
        last_pk = rows[-1]["pk"]
        for row in rows:
            expires_at = row["created_at"] + retention
            changes = {
                "content_expires_at": expires_at,
                "output_text": "",
            }
            if expires_at <= now:
                changes.update(
                    output_ciphertext="",
                    redaction_map="",
                    content_purged_at=now,
                )
                purged += 1
            else:
                legacy_output = row["output_text"] or ""
                changes["output_ciphertext"] = legacy_output
                if row["status"] in terminal:
                    changes["redaction_map"] = ""
                migrated += 1
                # Write ciphertext first and read it back through the historical
                # EncryptedTextField. Only erase the legacy plaintext after an
                # authenticated round-trip proves the value is recoverable.
                encrypted_changes = dict(changes)
                encrypted_changes.pop("output_text")
                AIRequest.objects.using(alias).filter(pk=row["pk"]).update(**encrypted_changes)
                verified = (
                    AIRequest.objects.using(alias)
                    .filter(pk=row["pk"])
                    .values_list("output_ciphertext", flat=True)
                    .get()
                )
                if verified != legacy_output:
                    raise RuntimeError("AI output encryption verification failed; plaintext retained.")
                changes = {"output_text": ""}
            AIRequest.objects.using(alias).filter(pk=row["pk"]).update(**changes)

    total = AIRequest.objects.using(alias).count()
    logger.warning(
        "AI attribution cutover quarantined=%s encrypted_active_content=%s purged_expired_content=%s",
        total,
        migrated,
        purged,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ai_app", "0014_seed_template_generation_prompt"),
        ("org", "0021_durable_center_settings"),
        ("parents", "0010_preserve_family_lifecycle_history"),
        ("students", "0011_protect_identity_history"),
        ("teachers", "0010_alter_payoutpolicy_method"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="airequest",
            name="attribution_status",
            field=models.CharField(
                choices=[("resolved", "Resolved"), ("unresolved", "Unresolved")],
                default="unresolved",
                editable=False,
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="airequest",
            name="authorization_permission",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="airequest",
            name="branch_at_request",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ai_requests",
                to="org.branch",
            ),
        ),
        migrations.AddField(
            model_name="airequest",
            name="content_expires_at",
            field=models.DateTimeField(
                db_index=True,
                default=apps.ai.models.ai_content_expiry,
                editable=False,
            ),
        ),
        migrations.AddField(
            model_name="airequest",
            name="content_purged_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="airequest",
            name="department_at_request",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ai_requests",
                to="org.department",
            ),
        ),
        migrations.AddField(
            model_name="airequest",
            name="output_ciphertext",
            field=core.fields.EncryptedTextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="airequest",
            name="parameter_fingerprint",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="airequest",
            name="provider_attempt_id",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="airequest",
            name="provider_attempted_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="airequest",
            name="provider_request_id",
            field=models.CharField(blank=True, editable=False, max_length=255),
        ),
        migrations.AddField(
            model_name="airequest",
            name="provider_stop_reason",
            field=models.CharField(blank=True, editable=False, max_length=32),
        ),
        migrations.AddField(
            model_name="airequest",
            name="provider_reconciliation_status",
            field=models.CharField(
                blank=True,
                choices=[("not_charged", "Not charged"), ("charged", "Charged")],
                editable=False,
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="airequest",
            name="provider_reconciliation_reference",
            field=models.CharField(blank=True, editable=False, max_length=128),
        ),
        migrations.AddField(
            model_name="airequest",
            name="provider_reconciled_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="airequest",
            name="requested_principal_id",
            field=models.PositiveBigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="airequest",
            name="requested_principal_kind",
            field=models.CharField(blank=True, editable=False, max_length=16),
        ),
        migrations.AddField(
            model_name="airequest",
            name="scope_status",
            field=models.CharField(
                choices=[
                    ("organization", "Organization-wide"),
                    ("resolved", "Resolved"),
                    ("unresolved", "Unresolved"),
                ],
                default="unresolved",
                editable=False,
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="airequest",
            name="output_text",
            field=models.TextField(blank=True, default="", editable=False),
        ),
        migrations.AlterField(
            model_name="airequest",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="airequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("denied_budget", "Denied (budget)"),
                    ("uncertain", "Provider outcome requires review"),
                ],
                db_index=True,
                default="queued",
                max_length=16,
            ),
        ),
        migrations.RunPython(protect_legacy_content),
        migrations.AlterField(
            model_name="airequest",
            name="error_detail",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="tenantaibudget",
            constraint=models.CheckConstraint(
                condition=models.Q(monthly_token_limit__gte=models.F("daily_token_limit")),
                name="ai_budget_monthly_not_below_daily",
            ),
        ),
        migrations.AddConstraint(
            model_name="aiprompt",
            constraint=models.CheckConstraint(
                condition=models.Q(effort__in=("low", "medium", "high", "max")),
                name="ai_prompt_effort_supported",
            ),
        ),
        migrations.AddConstraint(
            model_name="aiprompt",
            constraint=models.CheckConstraint(
                condition=models.Q(max_output_tokens__gt=0),
                name="ai_prompt_output_tokens_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="aiprompt",
            constraint=models.CheckConstraint(
                condition=models.Q(token_cost_cap__gte=models.F("max_output_tokens")),
                name="ai_prompt_cost_cap_covers_output",
            ),
        ),
        migrations.AddIndex(
            model_name="airequest",
            index=models.Index(
                fields=["scope_status", "branch_at_request", "department_at_request", "created_at"],
                name="ai_request_scope_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="airequest",
            index=models.Index(
                fields=["requested_principal_kind", "requested_principal_id", "created_at"],
                name="ai_request_principal_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status="resolved",
                        requested_by__isnull=False,
                        requested_principal_kind__in=("staff", "teacher", "student", "parent"),
                        requested_principal_id__isnull=False,
                        scope_status__in=("organization", "resolved"),
                    )
                    & ~models.Q(authorization_permission="")
                    | models.Q(
                        attribution_status="unresolved",
                        requested_principal_kind="",
                        requested_principal_id__isnull=True,
                        authorization_permission="",
                        scope_status="unresolved",
                    )
                ),
                name="ai_request_attribution_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        scope_status="organization",
                        branch_at_request__isnull=True,
                        department_at_request__isnull=True,
                    )
                    | models.Q(scope_status="resolved", branch_at_request__isnull=False)
                    | models.Q(
                        scope_status="unresolved",
                        branch_at_request__isnull=True,
                        department_at_request__isnull=True,
                    )
                ),
                name="ai_request_scope_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=models.Q(output_text=""),
                name="ai_request_no_plaintext_output",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=models.Q(cost_microusd__gte=0),
                name="ai_request_cost_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(provider_attempt_id="", provider_attempted_at__isnull=True)
                    | (~models.Q(provider_attempt_id="") & models.Q(provider_attempted_at__isnull=False))
                ),
                name="ai_request_provider_attempt_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(provider_request_id="")
                    | (
                        ~models.Q(provider_attempt_id="")
                        & models.Q(provider_attempted_at__isnull=False)
                        & models.Q(
                            provider_stop_reason__in=(
                                "end_turn",
                                "max_tokens",
                                "stop_sequence",
                                "refusal",
                            )
                        )
                    )
                ),
                name="ai_request_provider_receipt_has_attempt",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(~models.Q(provider_request_id="") | models.Q(provider_stop_reason="")),
                name="ai_request_stop_reason_requires_receipt",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        provider_reconciliation_status="",
                        provider_reconciliation_reference="",
                        provider_reconciled_at__isnull=True,
                    )
                    | (
                        models.Q(
                            provider_reconciliation_status__in=("not_charged", "charged"),
                            provider_reconciled_at__isnull=False,
                        )
                        & ~models.Q(provider_reconciliation_reference="")
                    )
                ),
                name="ai_request_reconciliation_evidence",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(provider_reconciliation_status="not_charged") | models.Q(provider_request_id="")
                ),
                name="ai_request_not_charged_has_no_receipt",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(provider_reconciliation_status="charged") | ~models.Q(provider_request_id="")
                ),
                name="ai_request_charged_has_receipt",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(status="uncertain")
                    | (
                        ~models.Q(provider_attempt_id="")
                        & models.Q(provider_request_id="")
                        & models.Q(reserved_tokens__gt=0)
                    )
                ),
                name="ai_request_uncertain_reserves_budget",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        status="queued",
                        reserved_tokens__gt=0,
                        provider_attempt_id="",
                        provider_request_id="",
                    )
                    | (models.Q(status="running", provider_request_id="") & models.Q(reserved_tokens__gt=0))
                    | (
                        models.Q(status="running")
                        & ~models.Q(provider_request_id="")
                        & models.Q(reserved_tokens=0)
                    )
                    | models.Q(
                        status="uncertain",
                        reserved_tokens__gt=0,
                        provider_request_id="",
                    )
                    | models.Q(
                        status__in=("succeeded", "failed", "denied_budget"),
                        reserved_tokens=0,
                    )
                ),
                name="ai_request_reservation_state_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(provider_attempt_id="")
                    | ~models.Q(provider_request_id="")
                    | (models.Q(status__in=("running", "uncertain")) & models.Q(reserved_tokens__gt=0))
                    | models.Q(
                        status="failed",
                        reserved_tokens=0,
                        provider_reconciliation_status="not_charged",
                    )
                ),
                name="ai_request_ambiguous_attempt_not_released",
            ),
        ),
        migrations.RunSQL(CREATE_GUARDS_SQL, reverse_sql=DROP_GUARDS_SQL),
    ]
