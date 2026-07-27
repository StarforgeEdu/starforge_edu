"""SMS campaign services (F10-1/2): build a campaign against a student segment (freezing
the recipient list + phones), then send it once via the Eskiz client; reusable templates.

Domain functions live here (imported by the layered services in ``services/v1`` AND
externally: tests use ``_resolve_phone``/``request_template_generation``, and celery
``run_template_generation`` writes back via ``apply_generated_template``).
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.campaigns.models import Campaign, CampaignRecipient, DoNotContact, MessageTemplate
from apps.students.models import StudentProfile
from core.exceptions import NotFoundException, UnprocessableEntity, ValidationException
from infrastructure.sms.eskiz_client import get_sms_client


def _resolve_phone(student: StudentProfile) -> str:
    """The number a student's message goes to: the primary guardian's (else any
    guardian's), falling back to the student's own. Uses the prefetched guardian list
    so building a campaign stays a fixed number of queries."""
    guardians = list(student.guardians.all())
    guardians.sort(key=lambda g: not g.is_primary)  # primary first
    for g in guardians:
        parent_user = getattr(g.parent, "user", None)
        if parent_user and parent_user.phone:
            return parent_user.phone
    if student.user and student.user.phone:
        return student.user.phone
    return ""


def _segment_queryset(segment: dict, branch):
    qs = StudentProfile.objects.all()
    if branch is not None:
        qs = qs.filter(branch=branch)
    status = segment.get("status")
    if status is not None:
        if status not in StudentProfile.Status.values:
            raise ValidationException(
                _("Unknown student status in the segment."), code="segment_status_invalid"
            )
        qs = qs.filter(status=status)
    cohort = segment.get("cohort")
    if cohort is not None:
        # bool is a subclass of int — reject it so {"cohort": true} can't silently
        # become current_cohort_id=1 and mis-target an audience.
        if not isinstance(cohort, int) or isinstance(cohort, bool):
            raise ValidationException(_("segment.cohort must be a cohort id."), code="segment_cohort_invalid")
        qs = qs.filter(current_cohort_id=cohort)
    return qs


@transaction.atomic
def create_campaign(
    *, name: str, message: str, segment: dict | None, created_by, branch=None, scheduled_at=None
) -> Campaign:
    """Freeze the audience: resolve every student in the segment to a recipient row +
    phone now, so the campaign is an exact, auditable record even if the roster changes
    later. Recipients without any phone are marked SKIPPED up front (never silently
    dropped).

    ``scheduled_at`` (optional) defers the blast: the campaign is created DRAFT and the
    ``dispatch_due_campaigns`` beat task sends it once the time arrives. A null means the
    campaign is sent manually via the send endpoint (unchanged behaviour)."""
    segment = {k: v for k, v in (segment or {}).items() if k in ("status", "cohort") and v not in (None, "")}
    students = list(
        _segment_queryset(segment, branch).select_related("user").prefetch_related("guardians__parent__user")
    )
    campaign = Campaign.objects.create(
        name=name,
        message=message,
        segment=segment,
        branch=branch,
        created_by=created_by,
        scheduled_at=scheduled_at,
        total=len(students),
    )
    # Consent: a phone on the do-not-contact list is suppressed up front (a recipient is
    # never even queued for it), so an opted-out family is excluded by construction.
    suppressed = set(DoNotContact.objects.values_list("phone", flat=True))
    rows = []
    skipped = 0
    for student in students:
        phone = _resolve_phone(student)
        opted_out = bool(phone) and phone in suppressed
        skip = not phone or opted_out
        if skip:
            skipped += 1
        rows.append(
            CampaignRecipient(
                campaign=campaign,
                student=student,
                phone=phone,
                status=(CampaignRecipient.Status.SKIPPED if skip else CampaignRecipient.Status.PENDING),
                # distinguish a consent skip from a no-phone skip in the audit trail
                error=("do_not_contact" if opted_out else ""),
            )
        )
    CampaignRecipient.objects.bulk_create(rows)
    if skipped:
        campaign.skipped_count = skipped
        campaign.save(update_fields=["skipped_count", "updated_at"])
    return campaign


def send_campaign(*, campaign_id: int, actor=None) -> Campaign:
    """Send (or resume) a campaign. Three phases on purpose:

    1. CLAIM (a short locked transaction): flip DRAFT -> SENDING so a concurrent or
       retried send serialises on the lock and the loser sees SENDING; a terminal
       SENT/FAILED campaign 422s. A campaign already in SENDING (a previous run died
       mid-send) is RESUMABLE — re-invoking processes only its remaining PENDING rows.
    2. SEND (outside any transaction): the external SMS calls must NOT run inside a DB
       transaction — a rollback can't unsend a message, and a row lock must not be held
       across network I/O. Recipients are deduped by phone (siblings sharing a guardian
       are texted once), and neither a send NOR a save failure aborts the batch.
    3. FINALIZE: recompute counts from the persisted recipient rows, so the totals are
       correct even after a partial/earlier run; leftover PENDING keeps it SENDING.
    """
    R = CampaignRecipient.Status
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().filter(pk=campaign_id).first()
        if campaign is None:
            raise NotFoundException(_("Campaign not found."), code="campaign_not_found")
        if campaign.status not in (Campaign.Status.DRAFT, Campaign.Status.SENDING):
            raise UnprocessableEntity(_("This campaign has already been sent."), code="campaign_already_sent")
        if campaign.status == Campaign.Status.DRAFT:
            campaign.status = Campaign.Status.SENDING
            campaign.sent_by = actor
            campaign.save(update_fields=["status", "sent_by", "updated_at"])

    client = get_sms_client()
    # Phones already delivered (this run or a prior partial run) — never text twice.
    texted = set(campaign.recipients.filter(status=R.SENT).values_list("phone", flat=True))
    # Honour an opt-out recorded AFTER the build: a now-suppressed phone is skipped, not
    # sent (consent wins over a frozen recipient list).
    suppressed = set(DoNotContact.objects.values_list("phone", flat=True))
    for recipient in campaign.recipients.filter(status=R.PENDING):
        now = timezone.now()
        if recipient.phone in suppressed:
            # Claim atomically (PENDING -> SKIPPED) so a racing invocation processes it once.
            CampaignRecipient.objects.filter(pk=recipient.pk, status=R.PENDING).update(
                status=R.SKIPPED, error="do_not_contact"
            )
            continue
        # Per-recipient compare-and-swap CLAIM: flip PENDING -> SENT so exactly ONE worker
        # owns this recipient even though the send loop runs outside the campaign row lock.
        # The scheduled-dispatch beat can race a manual "send now" (or a redelivered task)
        # on the same SENDING campaign; without this claim BOTH loops would call client.send
        # for every recipient and the paid SMS provider would double-charge + double-text
        # (a consent breach). A racer's UPDATE affects 0 rows -> it skips. We claim to SENT
        # optimistically (never double-send is safer than never-under-send for paid SMS);
        # a send failure rolls the row to FAILED below.
        claimed = CampaignRecipient.objects.filter(pk=recipient.pk, status=R.PENDING).update(
            status=R.SENT, sent_at=now, error=""
        )
        if not claimed:
            continue  # another concurrent worker already claimed/sent this recipient
        try:
            if recipient.phone not in texted:  # siblings sharing a guardian -> texted once
                client.send(phone=recipient.phone, text=campaign.message)
                texted.add(recipient.phone)
        except Exception as exc:  # one bad recipient must not abort the batch
            CampaignRecipient.objects.filter(pk=recipient.pk).update(
                status=R.FAILED, error=str(exc)[:255], sent_at=now
            )

    by = {row["status"]: row["n"] for row in campaign.recipients.values("status").annotate(n=Count("id"))}
    campaign.sent_count = by.get(R.SENT, 0)
    campaign.failed_count = by.get(R.FAILED, 0)
    campaign.skipped_count = by.get(R.SKIPPED, 0)
    campaign.sent_at = timezone.now()
    if by.get(R.PENDING, 0):
        campaign.status = Campaign.Status.SENDING  # partial — still resumable
    elif campaign.sent_count == 0 and campaign.failed_count > 0:
        campaign.status = Campaign.Status.FAILED
    else:
        campaign.status = Campaign.Status.SENT
    campaign.save(
        update_fields=["sent_count", "failed_count", "skipped_count", "sent_at", "status", "updated_at"]
    )
    return campaign


def dispatch_due_campaigns() -> int:
    """Send every campaign whose scheduled send-time has arrived (F10-1 dynamic send date).

    Runs inside the current tenant schema (the beat task fans it out per Center). Picks up
    DRAFT campaigns with a non-null ``scheduled_at`` that is now in the past and sends each
    via ``send_campaign``. Double-send safety does NOT come from this filter alone (a
    dispatch can race a manual "send now" on the same campaign, and Celery may redeliver
    the task): it comes from ``send_campaign``'s per-recipient compare-and-swap claim, which
    guarantees each recipient's SMS is sent at most once even under concurrent invocations.
    One campaign's failure never aborts the rest. Returns the number of campaigns dispatched."""
    now = timezone.now()
    due_ids = list(
        Campaign.objects.filter(
            status=Campaign.Status.DRAFT, scheduled_at__isnull=False, scheduled_at__lte=now
        ).values_list("pk", flat=True)
    )
    dispatched = 0
    for campaign_id in due_ids:
        try:
            send_campaign(campaign_id=campaign_id)
            dispatched += 1
        except Exception:  # a single bad campaign must not stop the sweep
            continue
    return dispatched


# ---------------------------------------------------------------------------
# Message templates (F10-2) — reusable, AI-draftable campaign message texts
# ---------------------------------------------------------------------------

_MAX_TEMPLATE_CHARS = 2000  # an SMS-ish template stays short


@transaction.atomic
def create_template(*, name, category="", purpose="", created_by=None) -> MessageTemplate:
    return MessageTemplate.objects.create(
        name=name, category=category or "", purpose=purpose or "", created_by=created_by
    )


@transaction.atomic
def update_template(*, template_id: int, fields: dict) -> MessageTemplate:
    """Edit a template under a row lock + explicit update_fields (no stale full-row save)."""
    tpl = MessageTemplate.objects.select_for_update().filter(pk=template_id).first()
    if tpl is None:
        raise NotFoundException(_("Template not found."), code="template_not_found")
    editable = {k: v for k, v in fields.items() if k in ("name", "category", "purpose", "body", "is_active")}
    if editable:
        for key, value in editable.items():
            setattr(tpl, key, value)
        tpl.save(update_fields=[*editable.keys(), "updated_at"])
    return tpl


def request_template_generation(*, template: MessageTemplate, requested_by=None):
    """Ask the AI to draft the template's body from its purpose (low-cost). Budget-
    reserved + enqueued on commit; the task fills the body, which the staff then edits +
    reuses. Like every AI-gen feature the request is idempotent on its source — the AI
    drafts the body ONCE; to revise it, edit the body (PATCH) directly."""
    from apps.ai.models import AIFeature
    from apps.ai.services import active_prompt, check_and_reserve_budget
    from core.utils import current_schema

    prompt = active_prompt(AIFeature.TEMPLATE_GENERATION)
    ai_request = check_and_reserve_budget(
        feature=AIFeature.TEMPLATE_GENERATION,
        estimated_tokens=prompt.token_cost_cap,
        requested_by=requested_by,
        source_app="campaigns",
        source_id=template.id,
    )
    if getattr(ai_request, "_should_enqueue", False):
        schema = current_schema()
        params = {"template_id": template.id, "name": template.name, "purpose": template.purpose}
        transaction.on_commit(lambda: _enqueue_template_generation(ai_request.pk, params, schema))
    return ai_request


def _enqueue_template_generation(ai_request_id: int, params: dict, schema: str) -> None:
    from celery_tasks.ai_tasks import run_template_generation

    run_template_generation.delay(ai_request_id, params=params, _schema_name=schema)


@transaction.atomic
def apply_generated_template(*, template_id: int, output_text: str) -> bool:
    """Write the AI's drafted text onto the template's body (F10-2). Locked + bounded;
    a vanished template is a no-op (returns False). Idempotent — a retry re-writes the
    same body."""
    tpl = MessageTemplate.objects.select_for_update().filter(pk=template_id).first()
    if tpl is None:
        return False
    tpl.body = (output_text or "").strip()[:_MAX_TEMPLATE_CHARS]
    tpl.save(update_fields=["body", "updated_at"])
    return True
