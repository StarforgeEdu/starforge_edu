from django.db import migrations


def seed_catalogues(apps, schema_editor):
    PipelineStage = apps.get_model("crm", "PipelineStage")
    LeadSource = apps.get_model("crm", "LeadSource")
    for slug, name, category, position in (
        ("new", "New", "open", 10),
        ("contacted", "Contacted", "open", 20),
        ("qualified", "Qualified", "open", 30),
        ("application-ready", "Application ready", "open", 40),
        ("converted", "Converted", "won", 90),
        ("lost", "Lost", "lost", 100),
    ):
        PipelineStage.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "category": category,
                "position": position,
                "is_active": True,
            },
        )
    for slug, name in (
        ("walk-in", "Walk-in"),
        ("referral", "Referral"),
        ("website", "Website"),
        ("social", "Social media"),
        ("other", "Other"),
    ):
        LeadSource.objects.update_or_create(
            slug=slug,
            defaults={"name": name, "is_active": True},
        )


IMMUTABILITY_SQL = """
CREATE OR REPLACE FUNCTION crm_reject_history_change()
RETURNS trigger
LANGUAGE plpgsql
AS $crm$
BEGIN
    RAISE EXCEPTION 'CRM history is append-only'
        USING ERRCODE = '55000';
END;
$crm$;

CREATE TRIGGER crm_stage_history_immutable
BEFORE UPDATE OR DELETE ON crm_leadstagehistory
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE TRIGGER crm_touch_immutable
BEFORE UPDATE OR DELETE ON crm_leadtouch
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE TRIGGER crm_attribution_immutable
BEFORE UPDATE OR DELETE ON crm_leadattribution
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE TRIGGER crm_merge_immutable
BEFORE UPDATE OR DELETE ON crm_leadmerge
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE TRIGGER crm_idempotency_immutable
BEFORE UPDATE ON crm_crmidempotencyrecord
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE TRIGGER crm_pipeline_stage_no_delete
BEFORE DELETE ON crm_pipelinestage
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE TRIGGER crm_lead_source_no_delete
BEFORE DELETE ON crm_leadsource
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE TRIGGER crm_campaign_no_delete
BEFORE DELETE ON crm_acquisitioncampaign
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE OR REPLACE FUNCTION crm_guard_follow_up_change()
RETURNS trigger
LANGUAGE plpgsql
AS $crm$
BEGIN
    IF OLD.status <> 'pending'
       OR NEW.status NOT IN ('completed', 'cancelled')
       OR OLD.lead_id IS DISTINCT FROM NEW.lead_id
       OR OLD.due_at IS DISTINCT FROM NEW.due_at
       OR OLD.purpose IS DISTINCT FROM NEW.purpose
       OR OLD.assignee_id IS DISTINCT FROM NEW.assignee_id
       OR OLD.assignee_principal_kind IS DISTINCT FROM NEW.assignee_principal_kind
       OR OLD.assignee_principal_id IS DISTINCT FROM NEW.assignee_principal_id
       OR OLD.created_by_id IS DISTINCT FROM NEW.created_by_id
       OR OLD.created_by_principal_kind IS DISTINCT FROM NEW.created_by_principal_kind
       OR OLD.created_by_principal_id IS DISTINCT FROM NEW.created_by_principal_id
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
        RAISE EXCEPTION 'CRM follow-up schedule and attribution are immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$crm$;

CREATE TRIGGER crm_follow_up_guard
BEFORE UPDATE ON crm_leadfollowup
FOR EACH ROW EXECUTE FUNCTION crm_guard_follow_up_change();

CREATE TRIGGER crm_follow_up_no_delete
BEFORE DELETE ON crm_leadfollowup
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE OR REPLACE FUNCTION crm_guard_duplicate_review()
RETURNS trigger
LANGUAGE plpgsql
AS $crm$
BEGIN
    IF OLD.left_id IS DISTINCT FROM NEW.left_id
       OR OLD.right_id IS DISTINCT FROM NEW.right_id
       OR OLD.detected_at IS DISTINCT FROM NEW.detected_at
       OR OLD.status <> 'pending'
       OR (
           NEW.status = 'pending'
           AND (
               NEW.reviewed_by_id IS NOT NULL
               OR NEW.reviewed_by_principal_kind <> ''
               OR NEW.reviewed_by_principal_id IS NOT NULL
               OR NEW.reviewed_at IS NOT NULL
               OR NEW.rationale <> ''
           )
       )
       OR (
           NEW.status IN ('dismissed', 'merged')
           AND (
               OLD.score IS DISTINCT FROM NEW.score
               OR OLD.signals IS DISTINCT FROM NEW.signals
           )
       )
       OR NEW.status NOT IN ('pending', 'dismissed', 'merged')
    THEN
        RAISE EXCEPTION 'CRM duplicate evidence or review decision is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$crm$;

CREATE TRIGGER crm_duplicate_review_guard
BEFORE UPDATE ON crm_leadduplicatecandidate
FOR EACH ROW EXECUTE FUNCTION crm_guard_duplicate_review();

CREATE TRIGGER crm_duplicate_no_delete
BEFORE DELETE ON crm_leadduplicatecandidate
FOR EACH ROW EXECUTE FUNCTION crm_reject_history_change();

CREATE OR REPLACE FUNCTION crm_guard_student_lead_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $crm$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM crm_crmlead
        WHERE student_id = NEW.id
          AND state <> 'won'
    ) THEN
        RAISE EXCEPTION 'unconverted CRM lead requires a CRM lifecycle transition'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$crm$;

CREATE TRIGGER crm_student_lead_transition_guard
BEFORE UPDATE OF status ON students_studentprofile
FOR EACH ROW
WHEN (OLD.status = 'lead' AND NEW.status <> 'lead')
EXECUTE FUNCTION crm_guard_student_lead_transition();

CREATE OR REPLACE FUNCTION crm_verify_lead_lifecycle_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $crm$
BEGIN
    IF NEW.state = 'merged' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM crm_leadmerge
            WHERE duplicate_id = NEW.id
              AND canonical_id = NEW.canonical_lead_id
              AND created_at >= transaction_timestamp()
        ) THEN
            RAISE EXCEPTION 'merged CRM lead requires immutable merge evidence'
                USING ERRCODE = '55000';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1
        FROM crm_leadstagehistory
        WHERE lead_id = NEW.id
          AND to_stage_id = NEW.stage_id
          AND to_state = NEW.state
          AND loss_reason = NEW.loss_reason
          AND created_at >= transaction_timestamp()
    ) THEN
        RAISE EXCEPTION 'CRM lifecycle change requires immutable stage history'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$crm$;

CREATE TRIGGER crm_lead_lifecycle_evidence
AFTER UPDATE ON crm_crmlead
FOR EACH ROW
WHEN (
    OLD.stage_id IS DISTINCT FROM NEW.stage_id
    OR OLD.state IS DISTINCT FROM NEW.state
    OR OLD.loss_reason IS DISTINCT FROM NEW.loss_reason
    OR OLD.canonical_lead_id IS DISTINCT FROM NEW.canonical_lead_id
)
EXECUTE FUNCTION crm_verify_lead_lifecycle_evidence();
"""

REVERSE_IMMUTABILITY_SQL = """
DROP TRIGGER IF EXISTS crm_lead_lifecycle_evidence ON crm_crmlead;
DROP FUNCTION IF EXISTS crm_verify_lead_lifecycle_evidence();
DROP TRIGGER IF EXISTS crm_student_lead_transition_guard ON students_studentprofile;
DROP FUNCTION IF EXISTS crm_guard_student_lead_transition();
DROP TRIGGER IF EXISTS crm_duplicate_no_delete ON crm_leadduplicatecandidate;
DROP TRIGGER IF EXISTS crm_duplicate_review_guard ON crm_leadduplicatecandidate;
DROP FUNCTION IF EXISTS crm_guard_duplicate_review();
DROP TRIGGER IF EXISTS crm_follow_up_no_delete ON crm_leadfollowup;
DROP TRIGGER IF EXISTS crm_follow_up_guard ON crm_leadfollowup;
DROP FUNCTION IF EXISTS crm_guard_follow_up_change();
DROP TRIGGER IF EXISTS crm_campaign_no_delete ON crm_acquisitioncampaign;
DROP TRIGGER IF EXISTS crm_lead_source_no_delete ON crm_leadsource;
DROP TRIGGER IF EXISTS crm_pipeline_stage_no_delete ON crm_pipelinestage;
DROP TRIGGER IF EXISTS crm_idempotency_immutable ON crm_crmidempotencyrecord;
DROP TRIGGER IF EXISTS crm_merge_immutable ON crm_leadmerge;
DROP TRIGGER IF EXISTS crm_attribution_immutable ON crm_leadattribution;
DROP TRIGGER IF EXISTS crm_touch_immutable ON crm_leadtouch;
DROP TRIGGER IF EXISTS crm_stage_history_immutable ON crm_leadstagehistory;
DROP FUNCTION IF EXISTS crm_reject_history_change();
"""


class Migration(migrations.Migration):
    dependencies = [("crm", "0001_initial")]

    operations = [
        migrations.RunPython(seed_catalogues, migrations.RunPython.noop),
        migrations.RunSQL(IMMUTABILITY_SQL, REVERSE_IMMUTABILITY_SQL),
    ]
