"""Auth DOMAIN functions: username+password login helpers, password reset via OTP,
custom-session issuance/revocation.

These are the tested domain helpers the layered ``AuthService`` (services/v1/) reuses.
Login is username+password; OTP codes serve password reset only (not login). Auth is
custom session auth — ``issue_token`` creates a Session and returns its opaque key (kept
under this name so conftest ``as_user`` and other callers stay unchanged).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.auth.signals import login_failed, login_succeeded, otp_failed, otp_requested, otp_verified
from apps.users.models import OTP
from apps.users.services import bump_token_version, set_user_password
from core.exceptions import (
    AuthenticationException,
    ServiceUnavailableException,
    StarforgeError,
    StrOrPromise,
    ThrottledException,
    ValidationException,
)
from core.privacy import private_fingerprint
from core.utils import current_schema, generate_otp
from core.validators import normalize_phone
from infrastructure.email.email_client import send_email
from infrastructure.sms.eskiz_client import get_sms_client

if TYPE_CHECKING:
    from apps.users.models import User
else:
    User = get_user_model()

# Computed once; used to equalize timing when the username does not exist so
# login responses don't reveal which usernames are registered.
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password("starforge-timing-equalizer")
    return _DUMMY_HASH


# ---------------------------------------------------------------------------
# Login (username + password)
# ---------------------------------------------------------------------------


def login_with_password(*, username: str, password: str, ip: str = "", user_agent: str = "") -> User:
    """Authenticate username+password and return the User.

    Failures are indistinguishable to the caller (401 ``invalid_credentials``
    for unknown username, wrong password, and inactive account alike) and a
    dummy hash check keeps the unknown-username path timing-equivalent.
    """
    from django_tenants.utils import get_public_schema_name

    from core.exceptions import NotFoundException

    if current_schema() != get_public_schema_name():
        raise NotFoundException(code="not_found")
    username = username.strip()
    user = User.objects.filter(username=username).first()
    if user is None:
        check_password(password, _dummy_hash())  # constant-time-ish equalizer
        _fire_login_failed(username, ip, user_agent, reason="unknown_username")
        raise AuthenticationException(_("Invalid username or password."), code="invalid_credentials")

    if not user.check_password(password) or not user.is_active or not (user.is_staff or user.is_superuser):
        reason = "wrong_password" if user.is_active else "inactive_user"
        _fire_login_failed(username, ip, user_agent, reason=reason)
        raise AuthenticationException(_("Invalid username or password."), code="invalid_credentials")

    user.last_seen_at = timezone.now()
    user.save(update_fields=["last_seen_at"])
    login_succeeded.send(
        sender=User,
        username=username,
        user_id=user.pk,
        ip=ip,
        user_agent=user_agent,
        schema_name=current_schema(),
    )
    return user


def _role_account_models() -> dict:
    """The 4 authenticatable role accounts keyed by principal kind (role-native auth)."""
    from apps.org.models import StaffProfile
    from apps.parents.models import ParentProfile
    from apps.students.models import StudentProfile
    from apps.teachers.models import TeacherProfile

    return {
        "student": StudentProfile,
        "teacher": TeacherProfile,
        "parent": ParentProfile,
        "staff": StaffProfile,
    }


def find_role_account(username: str):
    """The single role account (student/teacher/parent/staff) with this username, or
    ``(None, None)``.

    Cross-table uniqueness is a service invariant rather than one database
    constraint. Query every table so imports/races that violate it fail closed
    instead of authenticating whichever model happens to be searched first.
    The fixed four-query shape also avoids exposing the matching role table via
    early-return timing on an incorrect-password attempt.
    """
    matches = []
    for kind, model in _role_account_models().items():
        account = model.objects.select_related("user").filter(username=username).first()
        if account is not None:
            matches.append((kind, account))
    return matches[0] if len(matches) == 1 else (None, None)


def _has_privileged_bridge(account) -> bool:
    """Whether a role profile is attached to a Django-admin principal.

    Platform administrators authenticate only through Django's admin/User surface.  A
    role endpoint must never mint or reset credentials for their compatibility link,
    otherwise the resulting role session inherits ``is_superuser`` authorization.
    """
    return bool(account.user.is_staff or account.user.is_superuser)


def role_login(
    *,
    username: str,
    password: str,
    ip: str = "",
    user_agent: str = "",
    device_id: str = "",
    platform: str = "",
) -> dict:
    """Authenticate against the role table's own password and issue a role session.

    The session retains an internal User bridge for the existing permission/audit graph,
    but that bridge is not a usable login account and is never exposed to operators.
    """
    from core.session_auth import create_session

    username = username.strip()
    kind, account = find_role_account(username)
    if account is None:
        check_password(password, _dummy_hash())  # constant-time-ish equalizer
        _fire_login_failed(username, ip, user_agent, reason="unknown_username")
        raise AuthenticationException(_("Invalid username or password."), code="invalid_credentials")
    # Role accounts own their credentials. The linked User is only the hidden authorization
    # principal needed by the existing permission and session graph.
    password_matches = account.check_password(password)
    if (
        not password_matches
        or not account.is_active
        or not account.user.is_active
        or _has_privileged_bridge(account)
    ):
        reason = "wrong_password" if password_matches else "invalid_credentials"
        _fire_login_failed(username, ip, user_agent, reason=reason)
        raise AuthenticationException(_("Invalid username or password."), code="invalid_credentials")

    now = timezone.now()
    type(account).objects.filter(pk=account.pk).update(last_login_at=now)
    account.user.last_seen_at = now
    account.user.save(update_fields=["last_seen_at"])
    from apps.users.models import Device
    from apps.users.services import register_device

    normalized_device_id = device_id[:128]
    was_known_device = bool(
        normalized_device_id
        and platform
        and Device.objects.filter(
            user=account.user,
            device_id=normalized_device_id,
            revoked_at__isnull=True,
        ).exists()
    )
    device = register_device(
        user=account.user,
        device_id=normalized_device_id,
        platform=platform,
        user_agent=user_agent,
    )
    login_succeeded.send(
        sender=User,
        username=username,
        user_id=account.user_id,
        ip=ip,
        user_agent=user_agent,
        device_id=device.device_id if device is not None else "",
        is_new_device=device is not None and not was_known_device,
        principal_kind=kind,
        principal_id=account.pk,
        schema_name=current_schema(),
    )
    session = create_session(
        account.user,
        ip=ip,
        user_agent=user_agent,
        device_id=normalized_device_id,
        principal_kind=kind,
        principal_id=account.pk,
    )
    return {"access": session.key, "role": kind, "must_change_password": account.must_change_password}


def change_password(*, user: User, old_password: str, new_password: str) -> dict[str, str]:
    """Verify the old password, set the new one (ending every other session by bumping
    tv), and return a fresh access token so THIS device stays logged in."""
    if not user.check_password(old_password):
        raise ValidationException(
            _("The current password is incorrect."),
            code="wrong_password",
            fields={"old_password": [_("The current password is incorrect.")]},
        )
    _validate_new_password(new_password, user)
    set_user_password(user, new_password)  # bumps tv -> every existing token dies
    user.refresh_from_db(fields=["token_version"])
    return issue_token(user)


def _validate_new_password(raw: str, user: User | None) -> None:
    # Reject an oversized value before any similarity/common-password work. This
    # bounds attacker-controlled validator cost and, critically, precedes hashing.
    if len(raw) > 128:
        raise ValidationException(
            _("Choose a stronger password."),
            code="weak_password",
            fields={"new_password": [str(_("Use no more than 128 characters."))]},
        )
    messages: list[StrOrPromise] = []
    if len(raw) < 10:
        messages.append(_("Use at least 10 characters."))
    try:
        validate_password(raw, user=user)
    except DjangoValidationError as exc:
        # Every configured Django validator runs through validate_password. Replace
        # only the configured minimum-length prose with our stable public contract;
        # preserve similarity/common/numeric (and any future configured validator)
        # messages exactly once.
        errors = getattr(exc, "error_list", ())
        if errors:
            for error in errors:
                if len(raw) < 10 and getattr(error, "code", "") == "password_too_short":
                    continue
                messages.extend(error.messages)
        else:  # defensive compatibility with alternate ValidationError shapes
            messages.extend(exc.messages)
    deduped = list(dict.fromkeys(str(message) for message in messages))
    if deduped:
        raise ValidationException(
            _("Choose a stronger password."),
            code="weak_password",
            fields={"new_password": deduped},
        )


def _fire_login_failed(username: str, ip: str, user_agent: str, *, reason: str) -> None:
    login_failed.send(
        sender=User,
        username=username,
        ip=ip,
        user_agent=user_agent,
        reason=reason,
        schema_name=current_schema(),
    )


# ---------------------------------------------------------------------------
# OTP machinery (password reset / contact verification — NOT login)
# ---------------------------------------------------------------------------


def _on_public_schema() -> bool:
    from django_tenants.utils import get_public_schema_name

    return current_schema() == get_public_schema_name()


def _otp_cooldown_seconds() -> int:
    """Resend cooldown — `CenterSettings.otp_cooldown_seconds` per tenant, the
    `OTP_COOLDOWN_SECONDS` setting on the public schema."""
    if _on_public_schema():
        return int(getattr(settings, "OTP_COOLDOWN_SECONDS", 60))
    from apps.org.selectors import get_center_settings

    return int(get_center_settings().otp_cooldown_seconds)


def _channel_for(identifier: str) -> str:
    return OTP.CHANNEL_EMAIL if "@" in identifier else OTP.CHANNEL_SMS


def _ensure_password_reset_channel_enabled(identifier: str) -> None:
    """Fail uniformly before account lookup when the requested transport is off.

    A provider guard inside ``send_otp`` alone would make a known identifier return
    503 while an unknown identifier still returned the anti-enumeration 202.  Keep
    this check independent of account existence and reuse it on confirmation so a
    previously issued capability cannot be consumed while recovery is disabled.
    """

    channel = _channel_for(identifier)
    setting_name = "EMAIL_ENABLED" if channel == OTP.CHANNEL_EMAIL else "SMS_ENABLED"
    if not getattr(settings, setting_name, True):
        raise ServiceUnavailableException(
            _("Password reset is temporarily unavailable."),
            code="password_reset_unavailable",
        )


def _normalize(identifier: str) -> str:
    if "@" in identifier:
        return identifier.lower().strip()
    return normalize_phone(identifier)


def _enforce_cooldown(
    identifier: str,
    *,
    purpose: str = "",
    target_kind: str = "",
    target_id: int | None = None,
) -> None:
    cooldown = _otp_cooldown_seconds()
    rows = OTP.objects.filter(identifier=identifier)
    if purpose:
        rows = rows.filter(
            purpose=purpose,
            target_kind=target_kind,
            target_id=target_id,
        )
    latest = rows.order_by("-created_at").values_list("created_at", flat=True).first()
    if latest is None:
        return
    elapsed = (timezone.now() - latest).total_seconds()
    if elapsed < cooldown:
        raise ThrottledException(_("Please wait before requesting another code."), wait=cooldown - elapsed)


def _enforce_ip_cap(ip: str, identifier: str) -> None:
    """Reject when one IP fans out across too many distinct identifiers per hour.

    The cache stores only HMAC references. One atomic ``add`` per IP/identifier
    pair prevents concurrent repeats from incrementing the distinct counter.
    Once tripped, a separate block marker prevents the over-limit identifier
    from succeeding on its second attempt.
    """
    if not ip:
        return
    cap = int(getattr(settings, "OTP_IP_DISTINCT_IDENTIFIER_CAP", 5))
    window = 60 * 60
    schema = current_schema()
    ip_ref = private_fingerprint(ip, namespace=f"otp-ip:{schema}")
    identifier_ref = private_fingerprint(identifier, namespace=f"otp-identifier:{schema}")
    blocked_key = f"otp_ip_distinct_blocked:{ip_ref}"
    if cache.get(blocked_key):
        raise ThrottledException(_("Too many requests from your network."), wait=window)
    pair_key = f"otp_ip_ident_seen:{ip_ref}:{identifier_ref}"
    if not cache.add(pair_key, 1, timeout=window):
        return
    from core.ratelimit import check_rate

    try:
        check_rate(scope="otp_ip_distinct", key=ip_ref, limit=cap, window=window)
    except ThrottledException:
        cache.set(blocked_key, 1, timeout=window)
        raise ThrottledException(_("Too many requests from your network."), wait=window) from None


@transaction.atomic
def send_otp(
    *,
    identifier: str,
    purpose: str,
    target_kind: str = "",
    target_id: int | None = None,
    ip: str = "",
    user_agent: str = "",
) -> OTP:
    """Generate, store (hashed), and dispatch an OTP. Cooldown + per-IP capped.

    Callers must pass an explicit purpose (reset/verify) — there is no login
    purpose anymore."""

    identifier = _normalize(identifier)
    channel = _channel_for(identifier)
    _ensure_password_reset_channel_enabled(identifier)

    _enforce_cooldown(
        identifier,
        purpose=purpose,
        target_kind=target_kind,
        target_id=target_id,
    )
    _enforce_ip_cap(ip, identifier)

    code = generate_otp(settings.OTP_LENGTH)
    otp = OTP.objects.create(
        identifier=identifier,
        channel=channel,
        purpose=purpose,
        target_kind=target_kind,
        target_id=target_id,
        code_hash=make_password(code),
        expires_at=timezone.now() + timedelta(seconds=settings.OTP_TTL_SECONDS),
    )

    # External dispatch is grandfathered inline for auth OTP (CODE-GUIDE §6).
    if channel == OTP.CHANNEL_SMS:
        get_sms_client().send(
            phone=identifier,
            text=f"Starforge code: {code}. Valid for {settings.OTP_TTL_SECONDS // 60} min.",
        )
    else:
        send_email(
            to=identifier,
            subject="Starforge verification code",
            body=f"Your code is {code}. Valid for {settings.OTP_TTL_SECONDS // 60} minutes.",
        )

    schema = current_schema()
    transaction.on_commit(
        lambda: otp_requested.send(
            sender=OTP,
            identifier=identifier,
            purpose=purpose,
            ip=ip,
            user_agent=user_agent,
            schema_name=schema,
        )
    )
    return otp


def verify_otp(
    *,
    identifier: str,
    code: str,
    purpose: str,
    target_kind: str = "",
    target_id: int | None = None,
    ip: str = "",
    user_agent: str = "",
    before_consume: Callable[[], None] | None = None,
    hide_failure_details: bool = False,
) -> None:
    """Verify an OTP and mark it consumed; raises on any failure.

    Failed attempts are persisted so the max-attempts cap actually bites, and
    ALL failure signals fire after the transaction commits (review fix: two of
    three failure paths previously fired inside a rolled-back transaction)."""

    identifier = _normalize(identifier)
    failure: tuple[str, type[StarforgeError], StrOrPromise] | None = None

    with transaction.atomic():
        otp = (
            OTP.objects.select_for_update()
            .filter(
                identifier=identifier,
                purpose=purpose,
                target_kind=target_kind,
                target_id=target_id,
                consumed_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .order_by("-created_at")
            .first()
        )
        if otp is None:
            # Match the expensive password-hash verification below so an absent
            # capability is not distinguishable by a cheap timing probe.
            check_password(code, _dummy_hash())
            failure = (
                "no_active_code",
                ValidationException,
                _("Code expired or never issued. Request a new one."),
            )
        elif otp.attempts >= settings.OTP_MAX_ATTEMPTS:
            check_password(code, _dummy_hash())
            failure = ("too_many_attempts", ThrottledException, _("Too many attempts. Request a new code."))
        else:
            otp.attempts += 1
            if check_password(code, otp.code_hash):
                if before_consume is not None:
                    before_consume()
                otp.consumed_at = timezone.now()
                otp.save(update_fields=["attempts", "consumed_at"])
            else:
                otp.save(update_fields=["attempts"])
                failure = ("wrong_code", ValidationException, _("Invalid code."))

    if failure is not None:
        reason, exc_class, detail = failure
        _fire_failed(identifier, ip, user_agent, reason=reason)
        if hide_failure_details:
            raise ValidationException(_("Invalid or expired code."))
        raise exc_class(detail)

    otp_verified.send(
        sender=OTP,
        identifier=identifier,
        purpose=purpose,
        ip=ip,
        user_agent=user_agent,
        schema_name=current_schema(),
    )


def request_password_reset(
    *, identifier: str, account_type: str = "", ip: str = "", user_agent: str = ""
) -> None:
    """Send a reset OTP if (and only if) an account matches the identifier.

    Unknown identifiers are silently accepted — no SMS is sent, no OTP row is
    created, and the response is indistinguishable (anti-enumeration). The
    per-IP distinct-identifier cap is enforced BEFORE the existence check so
    probing sweeps still get throttled."""
    identifier = _normalize(identifier)
    # This must precede `_resettable_account`: disabled recovery must answer
    # identically for known and unknown identifiers (no status/timing oracle).
    _ensure_password_reset_channel_enabled(identifier)
    # Distributed SMS-cost protection. Run before account lookup so known and
    # unknown identifiers remain indistinguishable, and hash PII in cache keys.
    from core.ratelimit import check_rate

    check_rate(
        scope="otp_identifier",
        key=private_fingerprint(identifier, namespace=f"otp-rate:{current_schema()}"),
        limit=settings.OTP_IDENTIFIER_RATE_LIMIT,
        window=settings.OTP_IDENTIFIER_RATE_WINDOW_SECONDS,
    )
    check_rate(
        scope="otp_global",
        key="platform",
        limit=settings.OTP_GLOBAL_RATE_LIMIT,
        window=settings.OTP_GLOBAL_RATE_WINDOW_SECONDS,
    )
    _enforce_ip_cap(ip, identifier)
    account = _resettable_account(identifier, account_type=account_type)
    if account is None:
        return
    target_kind, target_id = _reset_principal(account)
    try:
        send_otp(
            identifier=identifier,
            purpose=OTP.PURPOSE_RESET,
            target_kind=target_kind,
            target_id=target_id,
            ip=ip,
            user_agent=user_agent,
        )
    except ThrottledException:
        # Anti-enumeration: an unknown identifier returns silently (202), so a
        # KNOWN identifier on its per-identifier OTP cooldown must NOT surface a
        # 429 — that 202-vs-429 difference was an account-existence oracle. Swallow
        # the cooldown here; the per-IP distinct-identifier cap (enforced above,
        # uniformly for known and unknown) and the view's per-identifier throttle
        # still bound abuse, and the existing valid code stays usable.
        return


def reset_password(
    *,
    identifier: str,
    code: str,
    new_password: str,
    account_type: str = "",
    ip: str = "",
    user_agent: str = "",
) -> None:
    """Complete a password reset: verify the OTP, set the password, end all
    sessions. The user logs in fresh with the new password afterwards."""
    identifier = _normalize(identifier)
    _ensure_password_reset_channel_enabled(identifier)
    user = _resettable_account(identifier, account_type=account_type)
    # Apply account-independent password policy first so universally weak input
    # behaves identically for known and unknown identifiers. Account-specific
    # similarity checks run only after the OTP itself proves account knowledge.
    _validate_new_password(new_password, None)
    if user is None:
        # Never match an old/unbound reset OTP.  This also keeps an invalid or
        # administrator-linked target indistinguishable from an unknown account.
        target_kind, target_id = "invalid", None
    else:
        target_kind, target_id = _reset_principal(user)

    def validate_for_verified_account() -> None:
        if user is not None:
            _validate_new_password(new_password, user)

    verify_otp(
        identifier=identifier,
        code=code,
        purpose=OTP.PURPOSE_RESET,
        target_kind=target_kind,
        target_id=target_id,
        ip=ip,
        user_agent=user_agent,
        before_consume=validate_for_verified_account,
        hide_failure_details=True,
    )
    if user is None:  # unreachable in practice: no OTP is issued for unknowns
        raise ValidationException(_("Invalid code."))
    if isinstance(user, User):
        set_user_password(user, new_password)
    else:
        from apps.users.services import set_role_account_password

        set_role_account_password(user, new_password, must_change=False)


def _find_by_identifier(identifier: str, *, account_type: str = ""):
    lookup = {"email": identifier} if "@" in identifier else {"phone": identifier}
    if account_type:
        model = _role_account_models().get(account_type)
        return model.objects.filter(**lookup).first() if model is not None else None
    role_matches = []
    for model in _role_account_models().values():
        role_matches.extend(model.objects.filter(**lookup)[:2])
        if len(role_matches) > 1:
            return None  # ambiguous contact: require account_type, never reset the wrong account
    if role_matches:
        account = role_matches[0]
        # Pre-cutover bridge rows can still mirror the same contact. Treat that as one
        # logical account, but reject if a different platform User owns it.
        if User.objects.filter(**lookup).exclude(pk=account.user_id).exists():
            return None
        return account
    users = list(User.objects.filter(**lookup)[:2])
    return users[0] if len(users) == 1 else None


def _resettable_account(identifier: str, *, account_type: str = ""):
    account = _find_by_identifier(identifier, account_type=account_type)
    if account is None or not account.is_active:
        return None
    if not isinstance(account, User) and (not account.user.is_active or _has_privileged_bridge(account)):
        return None
    return account


def _reset_principal(account) -> tuple[str, int]:
    if isinstance(account, User):
        return "user", account.pk
    for kind, model in _role_account_models().items():
        if isinstance(account, model):
            return kind, account.pk
    raise TypeError("Unsupported password-reset account type.")


def _fire_failed(identifier: str, ip: str, user_agent: str, *, reason: str) -> None:
    otp_failed.send(
        sender=OTP,
        identifier=identifier,
        ip=ip,
        user_agent=user_agent,
        reason=reason,
        schema_name=current_schema(),
    )


# ---------------------------------------------------------------------------
# Custom session auth (no JWT): the opaque session key is the whole credential
# ---------------------------------------------------------------------------


def issue_token(user: User, *, ip: str = "", user_agent: str = "", device_id: str = "") -> dict[str, str]:
    """Issue an auth session and return its opaque key as ``{"access": <key>}``.

    Custom session auth (no JWT library): the key IS the credential — tenant-bound by
    the schema it's created in, revocable via ``Session.revoked_at``, with roles read
    live each request (no claims, no token_version). Kept named ``issue_token`` so every
    caller (conftest ``as_user``, test helpers, the login view) works unchanged."""
    from core.session_auth import create_session

    session = create_session(user, ip=ip, user_agent=user_agent, device_id=device_id)
    return {"access": session.key}


def logout_everywhere(user: User) -> None:
    """Revoke EVERY session for the user (logout / password change / reset). Also bumps
    ``token_version`` (kept for any non-auth consumer); the live credential dies because
    its ``Session`` row is now revoked, rejected on the next request by session auth."""
    from core.session_auth import revoke_all_for_user

    revoke_all_for_user(user.pk)
    # Push tokens are credentials for private out-of-app content. Logging out
    # everywhere (also used by password change/reset) must revoke every device
    # registration instead of leaving a stale token eligible for delivery.
    from apps.users.models import Device

    Device.objects.filter(user_id=user.pk, revoked_at__isnull=True).update(
        revoked_at=timezone.now(),
        push_token="",
    )
    bump_token_version(user.pk)
    # TD-9: logout has no signal — audit it directly.
    from apps.audit.services import audit_log

    audit_log(actor=user, action="logout", resource_type="users.User", resource_id=str(user.pk))
