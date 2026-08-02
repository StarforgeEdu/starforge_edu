"""Tenancy services — write-side orchestration for tenant lifecycle."""

from __future__ import annotations

import ipaddress
import re
import secrets
import shlex
from datetime import timedelta
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_tenants.utils import get_public_schema_name, schema_context
from psycopg import sql

from apps.tenancy.models import Center, Domain, DomainClaim, PlatformEvent
from core.exceptions import NotFoundException, ValidationException

# Postgres-safe schema names: lowercase, starts with a letter, ≤ 63 chars.
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RESERVED_SLUGS = {"public", "admin", "www", "api", "static", "media"}

# Read-only impersonation tokens are deliberately short-lived (D4-LE-4).
IMPERSONATION_TOKEN_TTL_SECONDS = 600  # 10 minutes
DOMAIN_CHALLENGE_LABEL = "_starforge-verification"
DOMAIN_CHALLENGE_PREFIX = "starforge-domain-verification="
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_DNS_RESPONSE_BYTES = 64 * 1024


class _NoDNSRedirects(HTTPRedirectHandler):
    """DoH ownership checks never follow an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_DNS_OPENER = build_opener(_NoDNSRedirects())


def urlopen(request: Request, *, timeout: float):
    """Injectable no-redirect opener retained for focused tests."""

    return _DNS_OPENER.open(request, timeout=timeout)


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


def _normalize_domain(domain: str) -> str:
    """Return a canonical ASCII hostname or raise a field-safe 400."""
    if not isinstance(domain, str):
        raise ValidationException(_("Domain must be a hostname."), code="domain_invalid")
    value = domain.strip().lower().rstrip(".")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationException(_("Domain must be a valid hostname."), code="domain_invalid") from exc
    if not value or len(value) > 253 or "://" in value or "/" in value:
        raise ValidationException(_("Domain must be a valid hostname."), code="domain_invalid")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValidationException(_("An IP address cannot be verified as a domain."), code="domain_invalid")
    if any(_DOMAIN_LABEL_RE.fullmatch(label) is None for label in value.split(".")):
        raise ValidationException(_("Domain must be a valid hostname."), code="domain_invalid")
    return value


def _trusted_domain(domain: str) -> bool:
    """Whether the platform itself controls this suffix (no TXT challenge needed)."""
    suffixes = getattr(settings, "DOMAIN_VERIFICATION_TRUSTED_SUFFIXES", ())
    for raw_suffix in suffixes:
        suffix = str(raw_suffix).strip().lower().lstrip(".").rstrip(".")
        if suffix and (domain == suffix or domain.endswith(f".{suffix}")):
            return True
    return False


def _assert_operable_center(center: Center) -> None:
    """Reject control-plane writes to the public or an archived Center."""
    if center.schema_name == get_public_schema_name():
        raise ValidationException(_("The platform center cannot be mutated here."), code="public_center")
    if center.archived_at is not None:
        raise ValidationException(_("Archived centers are read-only."), code="center_archived")


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
    # A platform-owned hostname can route immediately. A custom hostname is a
    # pending claim and becomes primary only after its DNS TXT challenge passes.
    add_domain(center, domain=primary_domain, is_primary=True)

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
    _assert_operable_center(center)
    now = timezone.now()
    old_schema = center.schema_name
    prefix, suffix = "_archived_", f"_{now:%Y%m%d}"
    # Postgres identifiers max out at 63 bytes; truncate the slug portion so the
    # rename target and the varchar(63) schema_name column stay in sync.
    new_schema = f"{prefix}{old_schema[: 63 - len(prefix) - len(suffix)]}{suffix}"
    if len(new_schema) > 63:  # defensive backstop if the naming recipe changes
        raise RuntimeError("Archived schema name exceeds PostgreSQL's identifier limit.")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                    sql.Identifier(old_schema),
                    sql.Identifier(new_schema),
                )
            )
        center.schema_name = new_schema
        center.is_active = False
        center.archived_at = now
        center.save(update_fields=["schema_name", "is_active", "archived_at"])
    return center


@transaction.atomic
def set_primary_domain(center: Center, domain_id: int, *, actor=None) -> Domain:
    """Make exactly one Domain primary for a Center, atomically."""
    _assert_operable_center(center)
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
    record_platform_event(
        actor=actor,
        center=center,
        event=PlatformEvent.Event.DOMAIN_PRIMARY_CHANGED,
        payload={"domain_id": domain.pk, "domain": domain.domain},
    )
    return domain


@transaction.atomic
def add_domain(
    center: Center,
    *,
    domain: str,
    is_primary: bool = False,
    actor=None,
) -> Domain | DomainClaim:
    """Create a routable platform hostname or an isolated custom-domain claim.

    A custom hostname never exists in django-tenants' ``Domain`` table before
    its TXT proof succeeds. That remains safe while old application nodes serve
    during a rolling deploy and if the new image is rolled back later.
    """
    _assert_operable_center(center)
    domain = _normalize_domain(domain)
    if Domain.objects.filter(domain=domain).exists() or DomainClaim.objects.filter(domain=domain).exists():
        raise ValidationException(_("That domain is already registered."), code="domain_taken")
    trusted = _trusted_domain(domain)
    try:
        if trusted:
            row: Domain | DomainClaim = Domain.objects.create(
                domain=domain,
                tenant=center,
                is_primary=False,
            )
        else:
            row = DomainClaim.objects.create(
                domain=domain,
                tenant=center,
                verification_token=secrets.token_urlsafe(32),
                pending_primary=is_primary,
            )
    except IntegrityError as exc:
        # Unique race: a concurrent insert won between the pre-check and ours.
        raise ValidationException(_("That domain is already registered."), code="domain_taken") from exc
    if is_primary and trusted:
        if not isinstance(row, Domain):  # pragma: no cover - guarded by ``trusted``
            raise RuntimeError("Trusted domain creation returned an invalid record type.")
        set_primary_domain(center, row.pk, actor=actor)
        row.refresh_from_db()
    record_platform_event(
        actor=actor,
        center=center,
        event=PlatformEvent.Event.DOMAIN_ADDED,
        payload={
            "domain_id": str(row.pk) if isinstance(row, DomainClaim) else row.pk,
            "domain": row.domain,
            "is_verified": trusted,
            "pending_primary": bool(is_primary and not trusted),
        },
    )
    return row


def verify_domain_txt(domain: str, token: str) -> bool:
    """Check the domain's ownership challenge through DNS-over-HTTPS.

    Network/parser failures fail closed. The endpoint is fixed by settings (not
    user-controlled), the response is size-bounded, and only exact TXT values
    pass; a transient resolver failure can therefore never attach a hostname.
    """
    name = f"{DOMAIN_CHALLENGE_LABEL}.{_normalize_domain(domain)}"
    expected = f"{DOMAIN_CHALLENGE_PREFIX}{token}"
    return expected in _lookup_txt_records(name)


def _validated_dns_endpoint(endpoint: str, *, allow_query: bool = False) -> str | None:
    """Return an exact allowlisted HTTPS DoH endpoint or fail closed."""
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or len(endpoint) > 2048
        or "\\" in endpoint
        or any(ord(character) <= 32 or ord(character) == 127 for character in endpoint)
    ):
        return None
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower().rstrip(".")
    allowed_hosts = {
        str(value).strip().lower().rstrip(".")
        for value in getattr(settings, "DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS", ())
        if str(value).strip()
    }
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.query and not allow_query)
        or parsed.fragment
        or port not in (None, 443)
    ):
        return None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return endpoint
    return None


def _lookup_txt_records(name: str) -> tuple[str, ...]:
    endpoint = _validated_dns_endpoint(
        str(
            getattr(
                settings,
                "DOMAIN_VERIFICATION_DNS_URL",
                "https://cloudflare-dns.com/dns-query",
            )
        )
    )
    if endpoint is None:
        return ()
    timeout = float(getattr(settings, "DOMAIN_VERIFICATION_TIMEOUT_SECONDS", 3.0))
    separator = "&" if "?" in endpoint else "?"
    request = Request(
        f"{endpoint}{separator}{urlencode({'name': name, 'type': 'TXT'})}",
        headers={"Accept": "application/dns-json", "User-Agent": "Starforge-Domain-Verification/1"},
    )
    try:
        # URL scheme and hostname are exact-allowlisted above; the TXT name is
        # encoded only as a query value and cannot select another destination.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            if getattr(response, "status", 200) != 200:
                return ()
            final_url = response.geturl()
            if _validated_dns_endpoint(final_url, allow_query=True) is None:
                return ()
            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    content_length = int(raw_length)
                except (TypeError, ValueError):
                    return ()
                if content_length < 0 or content_length > _MAX_DNS_RESPONSE_BYTES:
                    return ()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in {"application/dns-json", "application/json"}:
                return ()
            raw = response.read(_MAX_DNS_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_DNS_RESPONSE_BYTES:
            return ()
        from infrastructure.http_client import strict_json_loads

        payload = strict_json_loads(raw)
    except (OSError, UnicodeError, ValueError):
        return ()
    if not isinstance(payload, dict) or payload.get("Status") != 0:
        return ()
    records: list[str] = []
    answers = payload.get("Answer") or ()
    if not isinstance(answers, list) or len(answers) > 256:
        return ()
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("type") != 16:
            continue
        data = answer.get("data")
        if not isinstance(data, str):
            continue
        try:
            # DNS JSON renders TXT chunks as ``"part1" "part2"``. A logical
            # TXT value is the concatenation of those chunks.
            records.append("".join(shlex.split(data)))
        except ValueError:
            continue
    return tuple(records)


def verify_domain(center: Center, *, claim_id, actor=None) -> Domain:
    """Promote one DNS-proven claim into the only routable domain table."""
    _assert_operable_center(center)
    claim = DomainClaim.objects.filter(tenant=center, pk=claim_id).first()
    if claim is None:
        raise NotFoundException(_("Domain claim does not belong to this center."))
    if claim.domain_record_id is not None:
        domain_record = Domain.objects.filter(pk=claim.domain_record_id).first()
        if domain_record is None:  # pragma: no cover - FK integrity backstop
            raise RuntimeError("Verified domain claim references a missing domain record.")
        return domain_record
    if not verify_domain_txt(claim.domain, claim.verification_token):
        raise ValidationException(
            _("DNS verification record was not found."),
            code="domain_verification_failed",
        )
    with transaction.atomic():
        # Lock only the claim row. Joining its nullable Domain relation under a
        # blanket FOR UPDATE is rejected by PostgreSQL (the nullable side of an
        # outer join cannot be locked).
        claim = DomainClaim.objects.select_for_update().get(tenant=center, pk=claim.pk)
        if claim.domain_record_id is not None:
            domain_record = Domain.objects.filter(pk=claim.domain_record_id).first()
            if domain_record is None:  # pragma: no cover - FK integrity backstop
                raise RuntimeError("Verified domain claim references a missing domain record.")
            return domain_record
        existing = Domain.objects.select_for_update().filter(domain=claim.domain).first()
        if existing is not None and existing.tenant_id != center.pk:
            raise ValidationException(
                _("That domain is already registered."),
                code="domain_taken",
            )
        row = existing or Domain.objects.create(
            domain=claim.domain,
            tenant=center,
            is_primary=False,
        )
        if claim.pending_primary:
            set_primary_domain(center, row.pk, actor=actor)
            row.refresh_from_db()
        claim.domain_record = row
        claim.verified_at = timezone.now()
        claim.pending_primary = False
        claim.save(update_fields=["domain_record", "verified_at", "pending_primary", "updated_at"])
        record_platform_event(
            actor=actor,
            center=center,
            event=PlatformEvent.Event.DOMAIN_VERIFIED,
            payload={"domain_id": row.pk, "domain": row.domain, "is_primary": row.is_primary},
        )
        return row


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
    """Suspend a Center and record how the suspension is enforced.

    A center with a subscription receives the normal billing paywall. Legacy or
    incompletely provisioned centers without one fall back to the hard inactive
    gate; returning success while leaving such a tenant accessible would make the
    control-plane action dangerously misleading.
    """
    _assert_operable_center(center)
    subscription_changed = _set_subscription_status(center, status="suspended")
    if not subscription_changed:
        # Provisioning deliberately does not fail when the plan catalogue is
        # temporarily unavailable.  A later platform suspension must still be
        # real: fall back to the hard inactive gate instead of returning a false
        # success while the tenant stays fully accessible.
        center.is_active = False
        center.save(update_fields=["is_active", "updated_at"])
    record_platform_event(
        actor=actor,
        center=center,
        event=PlatformEvent.Event.CENTER_SUSPENDED,
        payload={
            **({"reason": reason} if reason else {}),
            "enforcement": "subscription" if subscription_changed else "inactive_center",
        },
    )
    return center


@transaction.atomic
def activate_center(center: Center, *, actor=None) -> Center:
    """Re-activate a suspended Center: reactivate it AND flip its subscription
    back to ``active`` so the tenant API returns 200 again. Records a
    PlatformEvent."""
    _assert_operable_center(center)
    center.is_active = True
    update_fields = ["is_active", "updated_at"]
    if center.on_trial and (center.trial_ends_at is None or center.trial_ends_at <= timezone.now()):
        # Manual activation after an expired trial is a deliberate permanent
        # activation. Clear the expired marker so Beat cannot undo it next hour.
        center.on_trial = False
        update_fields.append("on_trial")
    center.save(update_fields=update_fields)
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
    _assert_operable_center(center)
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


def _set_subscription_status(center: Center, *, status: str) -> bool:
    """Drive the Day-3 billing state machine from the control center.

    Imported lazily: billing is a sibling SHARED app and reaching for it at
    module import time would couple the two lanes' load order. A center with no
    subscription row yet is a no-op (the paywall treats "no row" as pass-through).
    """
    from apps.billing.services import change_subscription

    try:
        change_subscription(center_id=center.pk, status=status)
    except NotFoundException:
        return False
    return True


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
    """Mint a 10-minute, read-only opaque session for ``user_id`` in ``center``.

    The row is tenant-schema-bound, records one exact role-native principal, and
    is centrally write-denied by ``core.session_auth.SessionAuthentication``.
    No refresh credential is minted, so impersonation cannot be extended.

    Writes BOTH audit trails (D4-LE-5): one public-schema PlatformEvent here,
    and one tenant-schema ``audit_log("impersonation.started")`` inside the
    target center's schema (so the school's own audit log shows it too).
    """
    _assert_operable_center(center)

    from apps.users.models import User

    with schema_context(center.schema_name):
        target = User.objects.filter(pk=user_id).first()
        if target is None:
            raise NotFoundException(_("No such user in that center."), code="user_not_found")
        principal_kind, principal_id = _unique_impersonation_principal(target)
        # A short-lived READ-ONLY session in the center's schema (custom session auth):
        # tenant-bound by the schema, read_only enforced by DenyWriteForReadOnlyToken,
        # and revocable. Cannot be extended (no refresh).
        from core.session_auth import create_session

        session = create_session(
            target,
            read_only=True,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )
        session.expires_at = timezone.now() + timedelta(seconds=IMPERSONATION_TOKEN_TTL_SECONDS)
        session.save(update_fields=["expires_at"])
        token_key = session.key
        # Tenant-side audit row: the school's own AuditLog records the access.
        _audit_impersonation_started(
            target=target,
            impersonator=impersonator,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )

    record_platform_event(
        actor=impersonator,
        center=center,
        event=PlatformEvent.Event.IMPERSONATION_MINTED,
        payload={
            "target_user_id": user_id,
            "principal_kind": principal_kind,
            "principal_id": principal_id,
            "read_only": True,
        },
    )
    return {"access": token_key, "expires_in": IMPERSONATION_TOKEN_TTL_SECONDS}


def _unique_impersonation_principal(target) -> tuple[str, int]:
    """Resolve exactly one active role account or reject the bridge target."""

    from django.apps import apps as django_apps

    from core.exceptions import ConflictException

    labels = {
        "student": "students.StudentProfile",
        "teacher": "teachers.TeacherProfile",
        "parent": "parents.ParentProfile",
        "staff": "org.StaffProfile",
    }
    active: list[tuple[str, int]] = []
    for kind, label in labels.items():
        model = django_apps.get_model(label)
        profile_id = (
            model.objects.filter(user_id=target.pk, is_active=True).values_list("pk", flat=True).first()
        )
        if profile_id is not None:
            active.append((kind, profile_id))
    if len(active) != 1 or not target.is_active or target.is_staff or target.is_superuser:
        raise ConflictException(
            _("The target account does not have one unambiguous active role."),
            code="impersonation_principal_ambiguous",
        )
    return active[0]


def _audit_impersonation_started(*, target, impersonator, principal_kind: str, principal_id: int) -> None:
    """Write the tenant-schema audit row (already inside schema_context)."""
    from apps.audit.models import AuditLog
    from apps.audit.services import audit_log

    audit_log(
        actor=None,  # the impersonator is a public-schema user, not a tenant FK
        action=AuditLog.Action.IMPERSONATE,
        resource_type="users.User",
        resource_id=str(target.pk),
        after={
            "impersonator_id": getattr(impersonator, "pk", None),
            "impersonator_repr": str(impersonator),
            "principal_kind": principal_kind,
            "principal_id": principal_id,
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
