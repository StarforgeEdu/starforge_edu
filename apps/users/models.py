"""Identity models: User, Device, OTP, RoleMembership.

Login is username + password (owner decision, 2026-06-11 — supersedes the
original OTP-as-login design). ``username`` is the unique identity; phone and
email are optional contact/verification channels. OTP codes now serve password
reset and contact verification only (see apps.auth).

Roles are assigned via RoleMembership(user, branch, department, role).
The Branch and Department FK targets live in apps.org (TENANT_APPS only),
so RoleMembership only exists on tenant schemas.
"""

from __future__ import annotations

from typing import ClassVar

from django.contrib.auth.hashers import check_password as _check_password
from django.contrib.auth.hashers import is_password_usable, make_password
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .managers import UserManager


class RoleAccount(models.Model):
    """Abstract login credentials for a role-native account.

    Each role (student / teacher / parent / staff) authenticates against its OWN table
    — the profile IS the account (role-native auth). Django's ``User`` is retained only
    for the ``/admin/`` panel; the app's login uses these fields. ``username`` is unique
    within each role table; global uniqueness across roles is enforced at create time.
    Passwords use Django's standard hashers, so the same policies/validators apply.
    """

    username = models.CharField(
        max_length=150,
        unique=True,
        null=True,
        blank=True,
        validators=[UnicodeUsernameValidator()],
        help_text=_("Login identifier for this role account."),
    )
    password = models.CharField(_("password"), max_length=128, blank=True)
    is_active = models.BooleanField(default=True)  # can this account sign in?
    must_change_password = models.BooleanField(default=False)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def set_password(self, raw_password: str) -> None:
        self.password = make_password(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return _check_password(raw_password, self.password)

    def set_unusable_password(self) -> None:
        self.password = make_password(None)

    def has_usable_password(self) -> bool:
        return is_password_usable(self.password)


class User(AbstractBaseUser, PermissionsMixin):
    class Gender(models.TextChoices):
        MALE = "m", _("Male")
        FEMALE = "f", _("Female")

    class Language(models.TextChoices):
        UZBEK = "uz", _("Uzbek")
        RUSSIAN = "ru", _("Russian")
        ENGLISH = "en", _("English")

    username = models.CharField(
        max_length=150,
        unique=True,
        validators=[UnicodeUsernameValidator()],
        help_text=_("Login identifier. Auto-generated when staff create accounts."),
    )
    phone = models.CharField(max_length=32, unique=True, null=True, blank=True)
    email = models.EmailField(unique=True, null=True, blank=True)

    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    middle_name = models.CharField(max_length=150, blank=True)

    birthdate = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=8, choices=Gender.choices, blank=True)
    preferred_language = models.CharField(max_length=8, choices=Language.choices, default=Language.UZBEK)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    # Bumped (F-expression) on password change, role grant/revoke, and
    # logout-everywhere. The JWT carries it as `tv`; a mismatch invalidates
    # every live access token (TD-1).
    token_version = models.PositiveIntegerField(default=1)

    date_joined = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS: ClassVar[list[str]] = []  # createsuperuser prompts username+password

    def __str__(self) -> str:  # pragma: no cover
        return self.username

    def get_full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)

    def get_short_name(self) -> str:
        return self.first_name or self.username


class OTP(models.Model):
    """One-time password sent via SMS or email.

    The raw code is never stored — only its hash. `attempts` tracks
    verify attempts to prevent brute force; consume() marks it used.
    """

    PURPOSE_VERIFY = "verify"
    PURPOSE_RESET = "reset"
    PURPOSE_CHOICES = [
        (PURPOSE_VERIFY, "Verify"),
        (PURPOSE_RESET, "Password reset"),
    ]

    CHANNEL_SMS = "sms"
    CHANNEL_EMAIL = "email"
    CHANNEL_CHOICES = [(CHANNEL_SMS, "SMS"), (CHANNEL_EMAIL, "Email")]

    identifier = models.CharField(max_length=255, db_index=True)  # phone or email
    channel = models.CharField(max_length=8, choices=CHANNEL_CHOICES)
    purpose = models.CharField(max_length=16, choices=PURPOSE_CHOICES, default=PURPOSE_VERIFY)
    code_hash = models.CharField(max_length=128)
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("identifier", "consumed_at"))]


class Device(models.Model):
    PLATFORM_WEB = "web"
    PLATFORM_IOS = "ios"
    PLATFORM_ANDROID = "android"
    PLATFORM_CHOICES = [
        (PLATFORM_WEB, "Web"),
        (PLATFORM_IOS, "iOS"),
        (PLATFORM_ANDROID, "Android"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    device_id = models.CharField(max_length=128)
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES)
    push_token = models.TextField(blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = (("user", "device_id"),)
        ordering = ("-last_seen_at",)


class Session(models.Model):
    """Custom auth session (no JWT): the opaque ``key`` is the Bearer token sent on
    every request. The row lives in the tenant schema, so a key only authenticates
    against the center that issued it — tenant binding is automatic, no token claim
    needed. Revocation is a row update (``revoked_at``); roles are read live per
    request, so a role change takes effect immediately (no stale-token window)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="auth_sessions")
    key = models.CharField(max_length=64, unique=True, db_index=True)
    # Role-native auth: which role account the caller signed in AS (student/teacher/parent/
    # staff). Blank for legacy User-based sessions. ``user`` stays the linked account so all
    # downstream authz + audit FKs are unchanged; the principal just records role identity.
    principal_kind = models.CharField(max_length=16, blank=True)
    principal_id = models.BigIntegerField(null=True, blank=True)
    ip_address = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    device_id = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    # D4-LE-4: an impersonation session is read-only (DenyWriteForReadOnlyToken
    # blocks writes under it). Replaces the old read_only JWT claim.
    read_only = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("user", "revoked_at"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"Session(user={self.user_id}, active={self.is_active})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()


class RoleMembership(models.Model):
    """Assignment of a Role to a User scoped by Branch (and optionally Department).

    Branch and Department live in apps.org (TENANT_APPS only). Because the
    `users` app is SHARED (TD-3 — public-schema platform staff), this model also
    gets a table in the public schema, where `org_*` tables do NOT exist. The
    `db_constraint=False` on the org FKs lets the public table be created without
    a dangling DB-level reference; tenant schemas keep referential integrity at
    the ORM/service layer (platform staff never hold RoleMemberships). See
    ADR-007.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="role_memberships")
    branch = models.ForeignKey(
        "org.Branch",
        on_delete=models.CASCADE,
        related_name="role_memberships",
        db_constraint=False,
    )
    department = models.ForeignKey(
        "org.Department",
        on_delete=models.CASCADE,
        related_name="role_memberships",
        null=True,
        blank=True,
        db_constraint=False,
    )
    role = models.CharField(max_length=32)

    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="grants_made",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = (("user", "branch", "department", "role"),)
        constraints = [
            models.UniqueConstraint(
                fields=("user", "branch", "role"),
                condition=models.Q(department__isnull=True),
                name="role_membership_unique_branch_role",
            ),
        ]
        ordering = ("-granted_at",)
