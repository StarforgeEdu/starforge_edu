"""Tenancy services — write-side orchestration for tenant lifecycle."""

from __future__ import annotations

import re
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_tenants.utils import schema_context

from apps.tenancy.models import Center, Domain, PlatformEvent
from core.exceptions import NotFoundException, ValidationException

# Postgres-safe schema names: lowercase, starts with a letter, ≤ 63 chars.
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RESERVED_SLUGS = {"public", "admin", "www", "api", "static", "media"}

# Read-only impersonation tokens are deliberately short-lived (D4-LE-4).
IMPERSONATION_TOKEN_TTL_SECONDS = 600  # 10 minutes


def _validate_slug(slug: str) -> str:
    slug = slug.lower().strip()
    if not SLUG_RE.match(slug):
        raise ValidationException(
            _("Slug must be lowercase letters, digits and underscores, starting with a letter."),
            code="slug_invalid",
        )
    if slug in RESERVED_SLUGS:
        raise ValidationException(_("That slug is reserved."), code="slug_reserved")
    if Center.objects.filter(slug=slug).exists() or Center.objects.filter(schema_name=slug).exists():
        raise ValidationException(_("That slug is already taken."), code="slug_taken")
    return slug


@transaction.atomic
def provision_center(
    *,
    name: str,
    slug: str,
    primary_domain: str,
    contact_name: str = "",
    contact_phone: str = "",
    contact_email: str = "",
) -> Center:
    """Create a Center + its primary Domain (triggers schema creation) and seed
    its CenterSettings singleton (TD-13)."""

    slug = _validate_slug(slug)

    center = Center.objects.create(
        name=name,
        slug=slug,
        schema_name=slug,
        contact_name=contact_name,
        contact_phone=contact_phone,
        contact_email=contact_email,
    )
    Domain.objects.create(domain=primary_domain, tenant=center, is_primary=True)

    # The schema + tenant tables now exist (auto_create_schema). Seed settings.
    with schema_context(center.schema_name):
        from apps.org.models import CenterSettings

        CenterSettings.load()

    return center


def delete_center(center: Center, *, force: bool = False) -> None:
    """Drop a Center and its schema. Refuses a populated tenant unless forced."""
    with schema_context(center.schema_name):
        from apps.users.models import User

        user_count = User.objects.count()
    if user_count > 0 and not force:
        raise ValidationException(
            _("Center still has users; pass force=True to delete."), code="center_not_empty"
        )
    center.delete(force_drop=True)


def archive_center(center: Center) -> Center:
    """Soft-archive: rename the schema out of the way and deactivate the Center.

    Uses raw `ALTER SCHEMA RENAME` (no ORM equivalent; the schema name is
    slug-validated so it is injection-safe — WORKLOG justification)."""
    if center.archived_at is not None:
        raise ValidationException(_("Center is already archived."), code="already_archived")
    now = timezone.now()
    old_schema = center.schema_name
    prefix, suffix = "_archived_", f"_{now:%Y%m%d}"
    # Postgres identifiers max out at 63 bytes; truncate the slug portion so the
    # rename target and the varchar(63) schema_name column stay in sync.
    new_schema = f"{prefix}{old_schema[: 63 - len(prefix) - len(suffix)]}{suffix}"
    assert len(new_schema) <= 63
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER SCHEMA "{old_schema}" RENAME TO "{new_schema}"')
        center.schema_name = new_schema
        center.is_active = False
        center.archived_at = now
        center.save(update_fields=["schema_name", "is_active", "archived_at"])
    return center


@transaction.atomic
def set_primary_domain(center: Center, domain_id: int) -> Domain:
    """Make exactly one Domain primary for a Center, atomically."""
    # Lock the whole domain set: locking only the target row lets two concurrent
    # promotions each demote a snapshot that misses the other's new primary.
    # The one_primary_domain_per_tenant constraint is the DB-level backstop.
    domains = {d.pk: d for d in Domain.objects.select_for_update().filter(tenant=center)}
    domain = domains.get(domain_id)
    if domain is None:
        raise NotFoundException(_("Domain does not belong to this center."))
    Domain.objects.filter(tenant=center, is_primary=True).exclude(pk=domain.pk).update(is_primary=False)
    domain.is_primary = True
    domain.save(update_fields=["is_primary"])
    return domain


@transaction.atomic
def add_domain(center: Center, *, domain: str, is_primary: bool = False) -> Domain:
    """Attach a hostname to a Center (TXT ownership check stubbed — O-8)."""
    if Domain.objects.filter(domain=domain).exists():
        raise ValidationException(_("That domain is already registered."), code="domain_taken")
    try:
        row = Domain.objects.create(domain=domain, tenant=center, is_primary=False)
    except IntegrityError as exc:
        # Unique race: a concurrent insert won between the pre-check and ours.
        raise ValidationException(_("That domain is already registered."), code="domain_taken") from exc
    if is_primary:
        set_primary_domain(center, row.pk)
        row.refresh_from_db()
    return row


def verify_domain_txt(domain: str) -> bool:
    """[OWNER:O-8] DNS TXT ownership verification — mock passes until creds land."""
    return True


# ---------------------------------------------------------------------------
# Platform event audit trail (D4-LE-5) — append-only, public schema
# ---------------------------------------------------------------------------
def record_platform_event(
    *,
    actor,
    center: Center | None,
    event: str,
    payload: dict | None = None,
) -> PlatformEvent:
    """Append one immutable PlatformEvent row (public schema).

    `actor` may be a public-schema platform-staff User or ``None`` (system).
    The row is never updated or deleted (there is no mutation API).
    """
    actor_instance = actor if getattr(actor, "pk", None) is not None else None
    return PlatformEvent.objects.create(
        actor=actor_instance,
        center=center,
        event=event,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# Center lifecycle (D4-LE-1) — suspend / activate / extend-trial
# ---------------------------------------------------------------------------
@transaction.atomic
def suspend_center(center: Center, *, actor=None, reason: str = "") -> Center:
    """Suspend a Center (billing): flip its subscription to ``suspended`` so the
    SubscriptionGateMiddleware paywall returns 402 on the API while STILL allowing
    auth/admin/healthz/schema (so the tenant can log in and pay). It does NOT set
    ``is_active=False``: that drives InactiveTenantMiddleware's 503 (which has no
    auth allowlist) and is reserved for hard archival / trial-expiry — otherwise
    the 503 would shadow the paywall and make the auth allowlist dead. Records a
    PlatformEvent. Idempotent."""
    _set_subscription_status(center, status="suspended")
    record_platform_event(
        actor=actor,
        center=center,
        event=PlatformEvent.Event.CENTER_SUSPENDED,
        payload={"reason": reason} if reason else {},
    )
    return center


@transaction.atomic
def activate_center(center: Center, *, actor=None) -> Center:
    """Re-activate a suspended Center: reactivate it AND flip its subscription
    back to ``active`` so the tenant API returns 200 again. Records a
    PlatformEvent."""
    center.is_active = True
    center.save(update_fields=["is_active", "updated_at"])
    _set_subscription_status(center, status="active")
    record_platform_event(
        actor=actor,
        center=center,
        event=PlatformEvent.Event.CENTER_ACTIVATED,
    )
    return center


@transaction.atomic
def extend_trial(center: Center, *, days: int, actor=None) -> Center:
    """Push `Center.trial_ends_at` out by `days` (from the later of now / the
    existing end). Records a PlatformEvent. Does not change subscription state —
    use activate_center for that."""
    if days <= 0:
        raise ValidationException(_("Days must be a positive integer."), code="invalid_days")
    now = timezone.now()
    base = center.trial_ends_at if (center.trial_ends_at and center.trial_ends_at > now) else now
    center.trial_ends_at = base + timedelta(days=days)
    center.on_trial = True
    center.save(update_fields=["trial_ends_at", "on_trial", "updated_at"])
    # Keep the billing subscription's trial period in lock-step with the trial end,
    # else the nightly meter suspends (402) the tenant at the ORIGINAL trial end even
    # though the InactiveTenant 503 gate honours the extension — the two gates would
    # disagree and the extension would be defeated. No-op if no/non-trialing sub.
    _extend_subscription_trial(center, new_trial_ends_at=center.trial_ends_at)
    record_platform_event(
        actor=actor,
        center=center,
        event=PlatformEvent.Event.CENTER_TRIAL_EXTENDED,
        payload={"days": days, "trial_ends_at": center.trial_ends_at.isoformat()},
    )
    return center


def _set_subscription_status(center: Center, *, status: str) -> None:
    """Drive the Day-3 billing state machine from the control center.

    Imported lazily: billing is a sibling SHARED app and reaching for it at
    module import time would couple the two lanes' load order. A center with no
    subscription row yet is a no-op (the paywall treats "no row" as pass-through).
    """
    from apps.billing.services import change_subscription

    try:
        change_subscription(center_id=center.pk, status=status)
    except NotFoundException:
        return  # no subscription row → nothing to flip (paywall passes through)


def _extend_subscription_trial(center: Center, *, new_trial_ends_at) -> None:
    """Sync the billing subscription's trial period to the center's new trial end.
    Lazy import (billing is a sibling SHARED app); no-op if there is no subscription."""
    from apps.billing.services import extend_trial_period

    try:
        extend_trial_period(center_id=center.pk, new_trial_ends_at=new_trial_ends_at)
    except NotFoundException:
        return


# ---------------------------------------------------------------------------
# Read-only impersonation (D4-LE-4/5)
# ---------------------------------------------------------------------------
@transaction.atomic
def mint_impersonation_token(*, center: Center, user_id: int, impersonator) -> dict:
    """Mint a 10-minute, read-only, access-ONLY JWT for `user_id` in `center`.

    Claims: ``{schema, impersonator_id, read_only: true, tv}`` — TD-1's auth
    class validates ``schema`` + ``tv``; ``read_only`` is enforced by
    ``core.permissions.DenyWriteForReadOnlyToken`` (see integration_needed).
    No refresh token is minted (impersonation cannot be extended).

    Writes BOTH audit trails (D4-LE-5): one public-schema PlatformEvent here,
    and one tenant-schema ``audit_log("impersonation.started")`` inside the
    target center's schema (so the school's own audit log shows it too).
    """
    from apps.users.models import User

    with schema_context(center.schema_name):
        target = User.objects.filter(pk=user_id).first()
        if target is None:
            raise NotFoundException(_("No such user in that center."), code="user_not_found")
        # A short-lived READ-ONLY session in the center's schema (custom session auth):
        # tenant-bound by the schema, read_only enforced by DenyWriteForReadOnlyToken,
        # and revocable. Cannot be extended (no refresh).
        from core.session_auth import create_session

        session = create_session(target, read_only=True)
        session.expires_at = timezone.now() + timedelta(seconds=IMPERSONATION_TOKEN_TTL_SECONDS)
        session.save(update_fields=["expires_at"])
        token_key = session.key
        # Tenant-side audit row: the school's own AuditLog records the access.
        _audit_impersonation_started(
            target=target,
            impersonator=impersonator,
        )

    record_platform_event(
        actor=impersonator,
        center=center,
        event=PlatformEvent.Event.IMPERSONATION_MINTED,
        payload={"target_user_id": user_id, "read_only": True},
    )
    return {"access": token_key, "expires_in": IMPERSONATION_TOKEN_TTL_SECONDS}


def _audit_impersonation_started(*, target, impersonator) -> None:
    """Write the tenant-schema audit row (already inside schema_context)."""
    from apps.audit.services import audit_log

    audit_log(
        actor=None,  # the impersonator is a public-schema user, not a tenant FK
        action="impersonation.started",
        resource_type="users.User",
        resource_id=str(target.pk),
        after={
            "impersonator_id": getattr(impersonator, "pk", None),
            "impersonator_repr": str(impersonator),
            "read_only": True,
        },
    )


# ---------------------------------------------------------------------------
# TD-19 tenant resolution (D4-LE-6) — anonymous, anon-throttled
# ---------------------------------------------------------------------------
def resolve_tenant(*, slug: str) -> dict:
    """Resolve a center slug to the public bootstrap payload a frontend needs to
    point itself at the right tenant (TD-19). Raises NotFoundException on an
    unknown / inactive / archived center."""
    center = (
        Center.objects.filter(slug=slug, is_active=True, archived_at__isnull=True)
        .prefetch_related("domains")
        .first()
    )
    if center is None:
        raise NotFoundException(_("No active center with that slug."), code="center_not_found")
    primary = next((d for d in center.domains.all() if d.is_primary), None)
    host = primary.domain if primary else ""
    scheme = "https"
    locale = _center_locale(center)
    return {
        "name": center.name,
        "base_url": f"{scheme}://{host}" if host else "",
        "ws_url": f"wss://{host}/ws/notifications/" if host else "",
        "logo": "",  # branding asset slot — populated when O-13 branding lands
        "locale": locale,
    }


def _center_locale(center: Center) -> str:
    """The center's default UI locale. Reads the tenant CenterSettings default
    language when set; falls back to the platform default. Best-effort: a center
    whose schema is mid-provision falls back without raising."""
    default = str(getattr(settings, "LANGUAGE_CODE", "uz")).split("-")[0]
    try:
        with schema_context(center.schema_name):
            from apps.org.selectors import get_center_settings

            value = getattr(get_center_settings(), "default_language", None)
            return value or default
    except Exception:
        return default
