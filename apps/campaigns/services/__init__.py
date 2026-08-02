"""SMS campaign services (F10-1/2): build a campaign against a student segment (freezing
the recipient list + phones), then send it once via the Eskiz client; reusable templates.

Domain functions live here (imported by the layered services in ``services/v1`` AND
externally: tests use ``_resolve_phone``/``request_template_generation``, and celery
``run_template_generation`` writes back via ``apply_generated_template``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.campaigns.models import Campaign, CampaignRecipient, DoNotContact, MessageTemplate
from apps.students.models import StudentProfile
from core.exceptions import (
    NotFoundException,
    ServiceUnavailableException,
    UnprocessableEntity,
    ValidationException,
)
from core.utils import current_schema
from infrastructure.sms.eskiz_client import get_sms_client

logger = logging.getLogger(__name__)

_CAMPAIGN_SEND_LEASE = timedelta(minutes=15)
_CAMPAIGN_DISPATCH_BATCH_SIZE = 100
_CAMPAIGN_HEARTBEAT_EVERY = 25


def _ensure_sms_enabled() -> None:
    if not getattr(settings, "SMS_ENABLED", True):
        raise ServiceUnavailableException(
            _("SMS campaigns are temporarily unavailable."),
            code="sms_unavailable",
        )


def _resolve_phone(student: StudentProfile) -> str:
    """The number a student's message goes to: the primary guardian's (else any
    guardian's), falling back to the student's own role-native contact. Revoked
    or inactive family accounts are excluded. Uses the prefetched active list so
    building a campaign stays a fixed number of queries."""
    guardians = getattr(student, "_active_campaign_guardians", None)
    if guardians is None:
        guardians = list(
            student.guardians.filter(
                revoked_at__isnull=True,
                parent__is_active=True,
                parent__user__is_active=True,
            ).select_related("parent")
        )
    guardians.sort(key=lambda g: not g.is_primary)  # primary first
    for g in guardians:
        if g.parent.is_active and g.parent.phone:
            return g.parent.phone
    if student.is_active and student.user.is_active and student.phone:
        return student.phone
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
    from apps.parents.models import Guardian

    students = list(
        _segment_queryset(segment, branch)
        .filter(is_active=True)
        .select_related("user")
        .prefetch_related(
            Prefetch(
                "guardians",
                queryset=Guardian.objects.filter(
                    revoked_at__isnull=True,
                    parent__is_active=True,
                    parent__user__is_active=True,
                ).select_related("parent"),
                to_attr="_active_campaign_guardians",
            )
        )
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
    """Atomically claim a campaign and enqueue its background delivery.

    A current SENDING lease makes repeated clicks/task redeliveries idempotent. A stale
    lease is replaced and resumed from the durable recipient rows. The provider loop is
    never executed in the HTTP request in production.
    """
    _ensure_sms_enabled()
    now = timezone.now()
    stale_before = now - _CAMPAIGN_SEND_LEASE
    claimed = False
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().filter(pk=campaign_id).first()
        if campaign is None:
            raise NotFoundException(_("Campaign not found."), code="campaign_not_found")
        if campaign.status == Campaign.Status.FAILED:
            # Provider failures can be ambiguous (a timeout may occur after Eskiz
            # accepted the SMS). Automatically retrying those rows could double-text
            # families, so expose an honest terminal error instead of claiming the
            # failed campaign was already sent.
            raise UnprocessableEntity(
                _("Campaign delivery failed and cannot be retried safely; create a new campaign."),
                code="campaign_delivery_failed",
            )
        if campaign.status not in (Campaign.Status.DRAFT, Campaign.Status.SENDING):
            raise UnprocessableEntity(_("This campaign has already been sent."), code="campaign_already_sent")
        has_live_lease = bool(
            campaign.status == Campaign.Status.SENDING
            and campaign.send_claim_token
            and campaign.send_heartbeat_at
            and campaign.send_heartbeat_at > stale_before
        )
        if not has_live_lease:
            campaign.status = Campaign.Status.SENDING
            campaign.send_claim_token = uuid.uuid4()
            campaign.send_claimed_at = now
            campaign.send_heartbeat_at = now
            campaign.send_attempts += 1
            campaign.last_error = ""
            if campaign.sent_by_id is None:
                campaign.sent_by = actor
            campaign.save(
                update_fields=[
                    "status",
                    "sent_by",
                    "send_claim_token",
                    "send_claimed_at",
                    "send_heartbeat_at",
                    "send_attempts",
                    "last_error",
                    "updated_at",
                ]
            )
            claimed = True

    if claimed:
        _enqueue_campaign_delivery(campaign)
        campaign.refresh_from_db()
    return campaign


def _enqueue_campaign_delivery(campaign: Campaign) -> None:
    from celery_tasks.campaign_tasks import deliver_campaign

    try:
        deliver_campaign.delay(
            campaign.pk,
            str(campaign.send_claim_token),
            _schema_name=current_schema(),
        )
    except Exception as exc:
        # The durable SENDING row is the outbox. Make the lease immediately stale so
        # the periodic dispatcher retries publication instead of stranding the blast.
        logger.warning(
            "Could not enqueue campaign %s delivery (%s)",
            campaign.pk,
            type(exc).__name__,
        )
        Campaign.objects.filter(
            pk=campaign.pk,
            status=Campaign.Status.SENDING,
            send_claim_token=campaign.send_claim_token,
        ).update(
            send_heartbeat_at=None,
            # Provider/broker messages can contain credentials, hosts, or payload
            # fragments. Keep those in restricted logs, never in product rows.
            last_error="queue_delivery_failed",
        )


def process_campaign_delivery(*, campaign_id: int, claim_token: str) -> str | None:
    """Deliver recipients only while ``claim_token`` owns the campaign lease.

    Recipient PENDING->SENT is an atomic compare-and-swap performed before the provider
    call. This deliberately preserves the existing at-most-once policy for paid SMS:
    after an ambiguous worker crash we may under-deliver, but never knowingly
    double-charge/double-text a family.
    """
    _ensure_sms_enabled()
    with _campaign_delivery_mutex(campaign_id=campaign_id) as acquired:
        if not acquired:
            # A redelivered copy of this task is already running. The live worker owns
            # the durable lease and will either finish or become recoverable after its
            # heartbeat expires.
            return None
        return _process_campaign_delivery_owned(
            campaign_id=campaign_id,
            claim_token=claim_token,
        )


@contextmanager
def _campaign_delivery_mutex(*, campaign_id: int) -> Iterator[bool]:
    """Prevent concurrent workers from delivering one campaign.

    Recipient CAS updates prevent duplicate *rows*, but two workers could otherwise
    claim different siblings that share a guardian phone and both send it. PostgreSQL
    session advisory locks are non-blocking, survive transaction boundaries used by the
    per-recipient writes, and are automatically released if a worker connection dies.
    """
    if connection.vendor != "postgresql":  # pragma: no cover - production is PostgreSQL
        yield True
        return

    key = f"campaign-delivery:{current_schema()}:{campaign_id}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", [key])
        acquired = bool(cursor.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [key])


def _process_campaign_delivery_owned(*, campaign_id: int, claim_token: str) -> str | None:
    campaign = Campaign.objects.filter(
        pk=campaign_id,
        status=Campaign.Status.SENDING,
        send_claim_token=claim_token,
    ).first()
    if campaign is None:
        return None

    R = CampaignRecipient.Status
    client = get_sms_client()
    texted = set(campaign.recipients.filter(status=R.SENT).values_list("phone", flat=True))
    suppressed = set(DoNotContact.objects.values_list("phone", flat=True))

    pending = campaign.recipients.filter(status=R.PENDING).only("id", "phone").iterator(chunk_size=100)
    for index, recipient in enumerate(pending):
        if index % _CAMPAIGN_HEARTBEAT_EVERY == 0 and not _heartbeat_campaign(
            campaign_id=campaign_id,
            claim_token=claim_token,
        ):
            return None

        now = timezone.now()
        if recipient.phone in suppressed:
            CampaignRecipient.objects.filter(pk=recipient.pk, status=R.PENDING).update(
                status=R.SKIPPED,
                error="do_not_contact",
            )
            continue

        recipient_claimed = CampaignRecipient.objects.filter(
            pk=recipient.pk,
            status=R.PENDING,
        ).update(
            status=R.SENT,
            sent_at=now,
            error="",
        )
        if not recipient_claimed:
            continue
        try:
            if recipient.phone not in texted:
                client.send(phone=recipient.phone, text=campaign.message)
                texted.add(recipient.phone)
        except Exception as exc:
            # Provider exception text and response bodies can contain phone
            # numbers, message fragments, or credentials. Keep only the class
            # and internal row identifiers in application logs.
            logger.warning(
                "Campaign %s recipient %s delivery failed (%s)",
                campaign_id,
                recipient.pk,
                type(exc).__name__,
            )
            CampaignRecipient.objects.filter(pk=recipient.pk).update(
                status=R.FAILED,
                error="delivery_failed",
                sent_at=now,
            )

    return _finalize_campaign_delivery(campaign_id=campaign_id, claim_token=claim_token)


def _heartbeat_campaign(*, campaign_id: int, claim_token: str) -> bool:
    return bool(
        Campaign.objects.filter(
            pk=campaign_id,
            status=Campaign.Status.SENDING,
            send_claim_token=claim_token,
        ).update(send_heartbeat_at=timezone.now())
    )


def _finalize_campaign_delivery(*, campaign_id: int, claim_token: str) -> str | None:
    R = CampaignRecipient.Status
    with transaction.atomic():
        campaign = (
            Campaign.objects.select_for_update()
            .filter(
                pk=campaign_id,
                status=Campaign.Status.SENDING,
                send_claim_token=claim_token,
            )
            .first()
        )
        if campaign is None:
            return None
        by = {row["status"]: row["n"] for row in campaign.recipients.values("status").annotate(n=Count("id"))}
        campaign.sent_count = by.get(R.SENT, 0)
        campaign.failed_count = by.get(R.FAILED, 0)
        campaign.skipped_count = by.get(R.SKIPPED, 0)
        campaign.send_claim_token = None
        campaign.send_heartbeat_at = None
        if by.get(R.PENDING, 0):
            campaign.status = Campaign.Status.SENDING
            campaign.last_error = "delivery_pending"
        elif campaign.sent_count == 0 and campaign.failed_count > 0:
            campaign.status = Campaign.Status.FAILED
            campaign.sent_at = timezone.now()
        else:
            campaign.status = Campaign.Status.SENT
            campaign.sent_at = timezone.now()
            campaign.last_error = ""
        campaign.save(
            update_fields=[
                "sent_count",
                "failed_count",
                "skipped_count",
                "sent_at",
                "status",
                "send_claim_token",
                "send_heartbeat_at",
                "last_error",
                "updated_at",
            ]
        )
        return campaign.status


def record_campaign_delivery_error(*, campaign_id: int, claim_token: str, error: Exception) -> None:
    logger.warning(
        "Campaign %s worker delivery failed (%s)",
        campaign_id,
        type(error).__name__,
    )
    Campaign.objects.filter(
        pk=campaign_id,
        status=Campaign.Status.SENDING,
        send_claim_token=claim_token,
    ).update(last_error="delivery_failed")


def dispatch_due_campaigns() -> int:
    """Queue due drafts and recover stale SENDING leases in a bounded sweep."""
    now = timezone.now()
    stale_before = now - _CAMPAIGN_SEND_LEASE
    due_ids = list(
        Campaign.objects.filter(
            Q(
                status=Campaign.Status.DRAFT,
                scheduled_at__isnull=False,
                scheduled_at__lte=now,
            )
            | Q(status=Campaign.Status.SENDING)
            & (
                Q(send_claim_token__isnull=True)
                | Q(send_heartbeat_at__isnull=True)
                | Q(send_heartbeat_at__lte=stale_before)
            )
        )
        .order_by("scheduled_at", "send_heartbeat_at", "pk")
        .values_list("pk", flat=True)[:_CAMPAIGN_DISPATCH_BATCH_SIZE]
    )
    dispatched = 0
    for campaign_id in due_ids:
        try:
            send_campaign(campaign_id=campaign_id)
            dispatched += 1
        except Exception:  # a single bad campaign must not stop the sweep
            logger.exception("Could not dispatch due campaign %s", campaign_id)
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


def request_template_generation(*, template: MessageTemplate, requested_by=None, requested_principal=None):
    """Ask the AI to draft the template's body from its purpose (low-cost). Budget-
    reserved + enqueued on commit; the task fills the body, which the staff then edits +
    reuses. Like every AI-gen feature the request is idempotent on its source — the AI
    drafts the body ONCE; to revise it, edit the body (PATCH) directly."""
    from apps.ai.models import AIFeature
    from apps.ai.services import check_and_reserve_budget
    from core.utils import current_schema

    ai_request = check_and_reserve_budget(
        feature=AIFeature.TEMPLATE_GENERATION,
        requested_by=requested_by,
        requested_principal=requested_principal,
        source_app="campaigns",
        source_id=template.id,
        params={"name": template.name, "purpose": template.purpose},
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
    tpl = MessageTemplate.objects.select_for_update().filter(pk=template_id, is_active=True).first()
    if tpl is None:
        return False
    tpl.body = (output_text or "").strip()[:_MAX_TEMPLATE_CHARS]
    tpl.save(update_fields=["body", "updated_at"])
    return True
