"""User-side write services."""

from __future__ import annotations

import random
import secrets
from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.users.models import Device
from core.exceptions import ValidationException
from core.utils import stable_hash
from core.validators import normalize_phone

_NAME_MAX = 150  # User first/last/middle_name column length
_USERNAME_MAX = 150
_MAX_PUSH_TOKEN_BYTES = 8 * 1024

if TYPE_CHECKING:
    from apps.users.models import User
else:
    User = get_user_model()


def resolve_or_create_user(
    *,
    phone: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    middle_name: str = "",
) -> User:
    """Find (or create, passwordless) a User by phone/email. Shared by the
    student/teacher creation services so identity handling stays in one place."""
    if phone:
        lookup = {"phone": normalize_phone(phone)}
    elif email:
        # This email becomes the account's unique login identifier — validate its
        # format and length up front rather than persisting junk (or 500ing on a
        # >254-char value that overflows the column).
        email = email.lower().strip()
        try:
            validate_email(email)
        except DjangoValidationError:
            raise ValidationException(
                _("Enter a valid email address."),
                code="validation_error",
                fields={"email": ["Enter a valid email address."]},
            ) from None
        lookup = {"email": email}
    else:
        raise ValidationException(_("phone or email is required."), code="identifier_required")
    for field, value in (("first_name", first_name), ("last_name", last_name), ("middle_name", middle_name)):
        if len(value) > _NAME_MAX:
            raise ValidationException(
                _("Name is too long."),
                code="validation_error",
                fields={field: [f"Must be at most {_NAME_MAX} characters."]},
            )
    user = User.objects.filter(**lookup).first()
    if user is None:
        user = User.objects.create(
            username=User.objects.generate_username(
                email or "", (phone or "").lstrip("+"), f"{first_name}.{last_name}"
            ),
            first_name=first_name,
            last_name=last_name,
            middle_name=middle_name,
            **lookup,
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


def prepare_role_identity(
    *,
    phone: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    middle_name: str = "",
) -> dict[str, str]:
    """Validate and normalize identity owned by a role profile.

    Role contacts intentionally do not live on the internal ``User`` bridge. This keeps
    student/teacher/parent/staff records independent while preserving the bridge only for
    the existing permission, session, and audit foreign keys.
    """
    phone = normalize_phone(phone) if phone else ""
    email = email.lower().strip()
    if email:
        try:
            validate_email(email)
        except DjangoValidationError:
            raise ValidationException(
                _("Enter a valid email address."),
                code="validation_error",
                fields={"email": ["Enter a valid email address."]},
            ) from None
    for field, value in (
        ("first_name", first_name),
        ("last_name", last_name),
        ("middle_name", middle_name),
    ):
        if len(value) > _NAME_MAX:
            raise ValidationException(
                _("Name is too long."),
                code="validation_error",
                fields={field: [f"Must be at most {_NAME_MAX} characters."]},
            )
    return {
        "phone": phone,
        "email": email,
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "middle_name": middle_name.strip(),
    }


def validate_role_username(username: str, *, bridge_user_id: int | None = None, account=None) -> str:
    """Return a normalized globally unique role username or raise a field error."""
    username = User.normalize_username(username.strip())
    if not username:
        return ""
    if len(username) > _USERNAME_MAX:
        raise ValidationException(
            _("Username is too long."),
            code="validation_error",
            fields={"username": [f"Must be at most {_USERNAME_MAX} characters."]},
        )
    try:
        UnicodeUsernameValidator()(username)
    except DjangoValidationError as exc:
        raise ValidationException(
            _("Enter a valid username."),
            code="validation_error",
            fields={"username": list(exc.messages)},
        ) from None
    taken = User.objects.filter(username=username)
    if bridge_user_id is not None:
        taken = taken.exclude(pk=bridge_user_id)
    if taken.exists():
        raise ValidationException(
            _("This username is already in use."),
            code="validation_error",
            fields={"username": ["This username is already in use."]},
        )
    from apps.auth.services import _role_account_models

    for model in _role_account_models().values():
        role_taken = model.objects.filter(username=username)
        if account is not None and isinstance(account, model) and account.pk:
            role_taken = role_taken.exclude(pk=account.pk)
        if role_taken.exists():
            raise ValidationException(
                _("This username is already in use."),
                code="validation_error",
                fields={"username": ["This username is already in use."]},
            )
    return username


@transaction.atomic
def create_role_user_bridge(
    *,
    username: str = "",
    phone: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    middle_name: str = "",
) -> tuple[User, str, dict[str, str]]:
    """Provision the hidden compatibility principal for a new role account.

    The role profile is the public account and owns all identity/login fields. The returned
    ``User`` has no contact identifiers and an unusable password, so it cannot be selected
    or used as a parallel login account. Its username only supplies a stable internal key
    for the pre-existing authorization graph.
    """
    identity = prepare_role_identity(
        phone=phone,
        email=email,
        first_name=first_name,
        last_name=last_name,
        middle_name=middle_name,
    )
    username = validate_role_username(username)
    if not username:
        username = User.objects.generate_username(
            identity["email"],
            identity["phone"].lstrip("+"),
            f"{identity['first_name']}.{identity['last_name']}",
        )
    user = User.objects.create_user(
        username=username,
        password=None,
        first_name=identity["first_name"],
        last_name=identity["last_name"],
        middle_name=identity["middle_name"],
        phone=None,
        email=None,
    )
    return user, username, identity


@transaction.atomic
def sync_role_user_bridge(account) -> None:
    """Keep an existing role account's hidden authorization principal coherent.

    This is used by Django admin. It never copies contact identifiers or passwords to
    ``User``; role credentials remain the sole usable login source.
    """
    if not account.user_id:
        user, username, _identity = create_role_user_bridge(
            username=account.username or "",
            phone=getattr(account, "phone", ""),
            email=getattr(account, "email", ""),
            first_name=getattr(account, "first_name", ""),
            last_name=getattr(account, "last_name", ""),
            middle_name=getattr(account, "middle_name", ""),
        )
        account.user = user
        account.username = username
        if not account.password:
            account.set_unusable_password()
        if not account.is_active:
            account.set_unusable_password()
            user.is_active = False
            user.save(update_fields=["is_active"])
        return

    username = validate_role_username(
        account.username or "",
        bridge_user_id=account.user_id,
        account=account,
    )
    if not username:
        username = User.objects.generate_username(
            getattr(account, "email", ""),
            getattr(account, "phone", "").lstrip("+"),
            f"{getattr(account, 'first_name', '')}.{getattr(account, 'last_name', '')}",
        )
        account.username = username
    user = account.user
    user.username = username
    user.first_name = getattr(account, "first_name", "")
    user.last_name = getattr(account, "last_name", "")
    user.middle_name = getattr(account, "middle_name", "")
    user.is_active = bool(account.is_active)
    # A role account must never retain a second usable password on its bridge.
    user.set_unusable_password()
    user.save(
        update_fields=[
            "username",
            "first_name",
            "last_name",
            "middle_name",
            "is_active",
            "password",
        ]
    )
    if not account.is_active:
        # Admin edits use this sync path directly.  Deactivation must revoke the
        # authorization graph as well as flipping two booleans.
        revoke_role_account_access(account)


@transaction.atomic
def update_role_identity(
    account,
    changes: dict,
    *,
    preferred_language: str | None = None,
) -> None:
    """Apply identity and bridge-owned preference changes atomically."""
    identity_fields = ("phone", "email", "first_name", "last_name", "middle_name")
    if any(field in changes for field in identity_fields):
        current = {field: changes.get(field, getattr(account, field)) for field in identity_fields}
        normalized = prepare_role_identity(**current)
        peers = type(account).objects.exclude(pk=account.pk)
        if (normalized["phone"] and peers.filter(phone=normalized["phone"]).exists()) or (
            normalized["email"] and peers.filter(email__iexact=normalized["email"]).exists()
        ):
            raise ValidationException(
                _("This contact already belongs to another account of this type."),
                code="duplicate_account",
            )
        for field in identity_fields:
            if field in changes:
                setattr(account, field, normalized[field])
    for field in ("birthdate", "gender", "is_active"):
        if field in changes:
            setattr(account, field, changes[field])
    # Preference-only updates belong to the compatibility user.  Avoid rewriting
    # the role account (and its password/encrypted identity columns) or revoking
    # access through bridge synchronization when none of those values changed.
    if changes:
        # Write only caller-supplied fields.  Saving the entire role row from a
        # request-scoped instance can overwrite an unrelated concurrent PATCH
        # (and rewrites encrypted columns even when their plaintext did not
        # change).  Keep ``updated_at`` accurate where the concrete profile has
        # that conventional field.
        update_fields = list(changes)
        if any(field.name == "updated_at" for field in account._meta.concrete_fields):
            update_fields.append("updated_at")
        account.save(update_fields=update_fields)
        sync_role_user_bridge(account)
    if preferred_language is not None and account.user.preferred_language != preferred_language:
        account.user.preferred_language = preferred_language
        account.user.save(update_fields=["preferred_language"])


@transaction.atomic
def revoke_role_account_access(account, *, deactivate_profile: bool = True) -> None:
    """Disable a role account's bridge and every credential/grant backed by it.

    The bridge is retained for immutable audit/history foreign keys, but it must not
    remain an active authorization principal after a profile is deactivated or deleted.
    This helper is also called by the profile ``pre_delete`` receivers, covering API,
    admin, and direct service deletion paths uniformly.
    """
    from apps.users.models import RoleMembership, Session

    now = timezone.now()
    if deactivate_profile and account.pk:
        account.is_active = False
        account.set_unusable_password()
        type(account).objects.filter(pk=account.pk).update(
            is_active=False,
            password=account.password,
        )

    user = User.objects.select_for_update().get(pk=account.user_id)
    user.is_active = False
    user.set_unusable_password()
    user.token_version += 1
    user.save(update_fields=["is_active", "password", "token_version"])
    account.user = user
    RoleMembership.objects.filter(user_id=account.user_id, revoked_at__isnull=True).update(revoked_at=now)
    Session.objects.filter(user_id=account.user_id, revoked_at__isnull=True).update(revoked_at=now)
    Device.objects.filter(user_id=account.user_id, revoked_at__isnull=True).update(
        revoked_at=now,
        push_token="",
    )


@transaction.atomic
def set_role_account_password(account, raw_password: str, *, must_change: bool = False) -> None:
    """Set the role-owned password and revoke every session for that account."""
    account.set_password(raw_password)
    account.must_change_password = must_change
    account.save(update_fields=["password", "must_change_password"])
    # Existing bridges may still carry a usable pre-cutover password. Disable it so
    # /auth/login cannot become a second route into the same role account.
    user = account.user
    user.set_unusable_password()
    user.save(update_fields=["password"])
    from core.session_auth import revoke_all_for_user

    revoke_all_for_user(account.user_id)


def issue_role_credentials(
    account,
    *,
    actor,
    resource_type: str,
    audit_scopes=None,
) -> dict[str, str | None]:
    """Generate and return a role account's one-time password exactly once."""
    from apps.audit.services import audit_log
    from core.exceptions import PermissionException

    if account.user.is_staff or account.user.is_superuser:
        raise PermissionException(
            _("Cannot replace a platform administrator password through a role endpoint."),
            code="forbidden",
        )
    temporary_password = generate_temp_password()
    set_role_account_password(account, temporary_password, must_change=True)
    scopes = tuple(audit_scopes) if audit_scopes is not None else (None,)
    for scope in scopes:
        audit_log(
            actor=actor,
            action="update",
            resource_type=resource_type,
            resource_id=str(account.pk),
            scope=scope,
        )
    return {"username": account.username, "temporary_password": temporary_password}


@transaction.atomic
def ensure_role_membership(
    account,
    *,
    branch,
    department=None,
    role: str | None = None,
    account_type=None,
    replace_scope: bool = True,
):
    """Keep a role profile's permission-native AccountType grant aligned.

    ``role`` is a compatibility input for callers that want the seeded system
    type. New admin code passes ``account_type`` directly. Explicit custom type
    memberships are never rewritten into a system type; only the selected type
    (or a legacy null row being upgraded) is touched.
    """
    from apps.access.models import AccountType
    from apps.users.models import RoleMembership

    if account_type is None:
        if not role:
            raise ValidationException(
                _("Choose an account type."),
                code="account_type_required",
                fields={"account_type": [_("This field is required.")]},
            )
        account_type = AccountType.objects.filter(
            is_system=True,
            is_active=True,
            slug=role,
        ).first()
        if account_type is None:
            raise ValidationException(
                _("The default account type is unavailable."),
                code="account_type_unavailable",
                fields={"account_type": [_("Activate the seeded account type first.")]},
            )
    else:
        account_type = AccountType.objects.filter(pk=account_type.pk, is_active=True).first()
        if account_type is None:
            raise ValidationException(
                _("Choose an active account type."),
                code="account_type_inactive",
                fields={"account_type": [_("Choose an active account type.")]},
            )

    kind_by_model = {
        "org.staffprofile": AccountType.AccountKind.STAFF,
        "teachers.teacherprofile": AccountType.AccountKind.TEACHER,
        "students.studentprofile": AccountType.AccountKind.STUDENT,
        "parents.parentprofile": AccountType.AccountKind.PARENT,
    }
    principal_kind = kind_by_model.get(account._meta.label_lower)
    if principal_kind is not None and account_type.account_kind != principal_kind:
        raise ValidationException(
            _("The account type does not match this profile."),
            code="principal_kind_mismatch",
            fields={"account_type": [_("Choose an account type for this profile kind.")]},
        )

    compatibility_role = account_type.compatibility_role
    department_id = department.pk if department is not None else None
    exact = RoleMembership.objects.filter(
        user=account.user,
        account_type=account_type,
        branch=branch,
    )
    exact = (
        exact.filter(department__isnull=True)
        if department_id is None
        else exact.filter(department_id=department_id)
    )
    exact = exact.order_by("revoked_at", "id")
    membership = exact.first()
    if membership is None:
        # Upgrade a pre-migration row in place before considering a new row.
        membership = (
            RoleMembership.objects.filter(
                user=account.user,
                account_type__isnull=True,
                role=compatibility_role,
                revoked_at__isnull=True,
            )
            .order_by("id")
            .first()
        )
    if membership is None and account_type.is_system and replace_scope:
        # A teacher/student/staff profile has one primary system type scope;
        # profile moves align that row. Parent guardian links pass
        # replace_scope=False because one parent can legitimately span branches.
        membership = (
            RoleMembership.objects.filter(
                user=account.user,
                account_type=account_type,
                revoked_at__isnull=True,
            )
            .order_by("id")
            .first()
        )
    if membership is None:
        return RoleMembership.objects.create(
            user=account.user,
            account_type=account_type,
            role=compatibility_role,
            branch=branch,
            department=department,
        )
    changed: list[str] = []
    if membership.account_type_id != account_type.pk:
        membership.account_type = account_type
        changed.append("account_type")
    if membership.role != compatibility_role:
        membership.role = compatibility_role
        changed.append("role")
    if membership.branch_id != branch.pk:
        membership.branch = branch
        changed.append("branch")
    if membership.department_id != department_id:
        membership.department = department
        changed.append("department")
    if membership.revoked_at is not None:
        membership.revoked_at = None
        changed.append("revoked_at")
    if changed:
        membership.save(update_fields=changed)
    return membership


@transaction.atomic
def bump_token_version(user_id: int) -> None:
    """Invalidate every live access token for a user (TD-1 `tv` claim)."""
    user = User.objects.select_for_update().get(pk=user_id)
    user.token_version += 1
    user.save(update_fields=["token_version"])


def set_user_password(user: User, raw_password: str) -> None:
    """Set a password and end EVERY existing session: all outstanding refresh
    tokens are blacklisted and `tv` is bumped so live access tokens die too.
    (Review fix: a tv bump alone left stolen refreshes valid for 14 days.)"""
    user.set_password(raw_password)
    user.save(update_fields=["password"])
    # Lazy import: apps.auth.services imports from this module (circular otherwise).
    from apps.auth.services import logout_everywhere

    logout_everywhere(user)


# Unambiguous alphabet for one-time passwords (no 0/O/1/I/l) — easy to read aloud/type.
_TEMP_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"
_TEMP_DIGITS = "23456789"


def generate_temp_password(length: int = 10) -> str:
    """A readable, strong one-time password (>=1 digit + letters -> clears the password
    validators; drops ambiguous characters for easy typing/hand-off)."""
    length = max(length, 8)
    chars = [secrets.choice(_TEMP_LETTERS) for _ in range(length - 2)]
    chars.append(secrets.choice(_TEMP_DIGITS))
    chars.append(secrets.choice(_TEMP_LETTERS))
    random.SystemRandom().shuffle(chars)
    return "".join(chars)


@transaction.atomic
def register_device(
    *,
    user: User,
    device_id: str,
    platform: str,
    user_agent: str = "",
    push_token: str = "",
) -> Device | None:
    """Upsert a Device on login / push-token registration. No-op without both
    a stable `device_id` and a `platform`."""
    if not device_id or not platform:
        return None
    # Truncate to the column bounds (device_id 128, platform 16) so a long client
    # value never 500s mid-login — mirrors core.session_auth.create_session.
    device_id, platform = device_id[:128], platform[:16]
    defaults: dict[str, object] = {
        "platform": platform,
        "user_agent": user_agent,
        "last_seen_at": timezone.now(),
        "revoked_at": None,
    }
    normalized_push_token = push_token.strip()
    if len(normalized_push_token.encode("utf-8")) > _MAX_PUSH_TOKEN_BYTES:
        raise ValidationException(
            _("Push token is too large."),
            fields={"push_token": [f"Must be at most {_MAX_PUSH_TOKEN_BYTES} bytes."]},
        )
    push_token_fingerprint = stable_hash(normalized_push_token) if normalized_push_token else ""
    if normalized_push_token:
        defaults["push_token"] = normalized_push_token

    # The partial unique constraint is the final cross-account privacy boundary.
    # If two accounts claim a newly-issued provider token concurrently, one may
    # discover the other only after its first write conflicts. Retry once in a
    # fresh savepoint, lock the now-visible owner, clear it, and let last writer
    # win without ever storing the same token on two accounts.
    for attempt in range(2):
        try:
            with transaction.atomic():
                if normalized_push_token:
                    conflicting_ids = list(
                        Device.objects.select_for_update()
                        .filter(push_token_fingerprint=push_token_fingerprint)
                        .exclude(user=user, device_id=device_id)
                        .values_list("pk", flat=True)
                    )
                    if conflicting_ids:
                        Device.objects.filter(pk__in=conflicting_ids).update(push_token="")
                device, _created = Device.objects.update_or_create(
                    user=user,
                    device_id=device_id,
                    defaults=defaults,
                )
                return device
        except IntegrityError:
            if not normalized_push_token or attempt == 1:
                raise
    raise AssertionError("unreachable device registration retry state")
