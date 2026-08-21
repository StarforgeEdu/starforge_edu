"""Audit receivers (TD-9, D3-D-2).

Connects `post_save` / `post_delete` (and a `pre_save` before-snapshot) to the
sensitive-model list below. Models are resolved via `apps.get_model` inside
`connect_audit_receivers()` (called from `AuditConfig.ready()`) wrapped in
try/except `LookupError`: sibling lanes build these apps the same day, so a
not-yet-migrated / not-yet-defined model must not crash app loading.

Audited models include identity/grants, organization policy and structure,
finance/payment state, published academic results, provider configuration, and
compensation approval history. ``AUDITED_MODELS`` below is authoritative.

`before` snapshots are captured in `pre_save` keyed by `(label, pk)` in a
thread-local map and consumed by the matching `post_save` so update rows carry a
real before/after diff. Sensitive fields are masked centrally by `audit_log`
(see `apps.audit.services.MASKED_FIELDS`).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from django.apps import apps
from django.core.signals import request_finished
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.audit.context import current_actor, current_request
from apps.audit.models import AuditLog
from apps.audit.scopes import audit_scope_for_instance
from apps.audit.services import audit_log, audit_log_on_commit, diff_snapshots, serialize_instance
from apps.auth.signals import (
    login_failed,
    login_succeeded,
    otp_failed,
    otp_requested,
    otp_verified,
)
from core.privacy import private_fingerprint
from core.role_principals import RolePrincipal
from core.utils import current_schema

logger = logging.getLogger("starforge.audit")

# (app_label, model_name) — resolved lazily so import order never matters.
AUDITED_MODELS: tuple[tuple[str, str], ...] = (
    ("users", "User"),
    ("users", "RoleMembership"),
    # Organization-scope mutations define who can see every downstream row.
    # Keep their soft-deactivation and configuration history immutable too.
    ("org", "Branch"),
    ("org", "Department"),
    ("org", "Room"),
    ("org", "BranchWorkingHours"),
    ("org", "BranchHoliday"),
    ("org", "CenterSettings"),
    ("finance", "Invoice"),
    ("payments", "Payment"),
    ("academics", "Grade"),
    ("academics", "ExamResult"),
    ("payments", "ProviderConfig"),
    # Compensation and its maker-checker state require a durable history. The
    # read repository applies a second, compensation-specific visibility gate.
    ("teachers", "TeacherProfile"),
    ("teachers", "PayoutPolicy"),
    ("approvals", "ApprovalRequest"),
    ("approvals", "LedgerEntry"),
    # A-2 changes live server-side authorization and require an append-only trail.
    ("access", "RolePermissionOverride"),
    # Leadership workflow state is decision evidence, not disposable UI state.
    # Keep the definition/status changes and the role-native attendee/assignee
    # attribution immutable; response answer values themselves are deliberately
    # excluded because they can contain free-form sensitive information.
    # These two apps intentionally use nonstandard Django labels to avoid
    # collisions with django.forms and Celery's generic tasks modules. Receiver
    # resolution must use AppConfig.label, not the Python package name.
    ("forms_app", "Form"),
    ("forms_app", "FormField"),
    ("meetings", "StaffMeeting"),
    ("meetings", "MeetingAttendee"),
    ("staff_tasks", "RoleGrade"),
    ("staff_tasks", "Task"),
)

# Stable dispatch_uids so re-imports (and the test suite's repeated ready())
# never double-register a receiver and write the row twice.
_PRE_UID = "audit.pre_save"
_POST_UID = "audit.post_save"
_DEL_UID = "audit.post_delete"

# Per-thread before-snapshot store: maps "schema:label:pk" -> field snapshot
# captured at pre_save, consumed by the following post_save in the same
# thread/request. The schema is part of the key so a stale entry left by a
# failed save in tenant A can never be popped by tenant B's save of the same
# label:pk on a reused (Celery/gunicorn) worker thread.
_before_store = threading.local()


def _label_for(sender: Any) -> str:
    return f"{sender._meta.app_label}.{sender.__name__}"


def _store_key(label: str, pk: Any) -> str:
    # Schema-scoped so a reused worker thread can't cross-pollinate tenants.
    return f"{current_schema()}:{label}:{pk}"


def _on_pre_save(sender: Any, instance: Any, **kwargs: Any) -> None:
    if instance.pk is None:
        return  # creation — no prior state to snapshot
    try:
        previous = sender.objects.filter(pk=instance.pk).first()
    except Exception:  # pragma: no cover - defensive; never break the save
        return
    if previous is None:
        return
    store = _before_store.__dict__.setdefault("data", {})
    store[_store_key(_label_for(sender), instance.pk)] = serialize_instance(previous)


def _on_post_save(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    label = _label_for(sender)
    key = _store_key(label, instance.pk)
    # ALWAYS pop our own pre_save entry, even on the created path: a create can
    # follow a failed update of the same pk (pre_save fired, the UPDATE raised,
    # post_save never ran) and the stale entry must not linger. try/finally is
    # not enough on its own — pop unconditionally so the store self-cleans.
    store = _before_store.__dict__.setdefault("data", {})
    before = store.pop(key, None)
    after = serialize_instance(instance)
    if created:
        audit_log_on_commit(
            actor=current_actor(),
            action=AuditLog.Action.CREATE,
            resource_type=label,
            resource_id=instance.pk,
            before=None,
            after=after,
            request=current_request(),
            scope=audit_scope_for_instance(instance, after=after),
        )
        return
    audit_log_on_commit(
        actor=current_actor(),
        action=AuditLog.Action.UPDATE,
        resource_type=label,
        resource_id=instance.pk,
        before=before,
        after=diff_snapshots(before, after) if before else after,
        request=current_request(),
        scope=audit_scope_for_instance(instance, before=before, after=after),
    )


@receiver(request_finished, dispatch_uid="audit.clear_before_store")
def _clear_before_store(sender: Any, **kwargs: Any) -> None:
    """Drop any before-snapshots left over by a failed save at request end.

    A pre_save whose DB write raised before post_save fired leaves its entry in
    the thread-local. Worker/gunicorn threads are long-lived, so clear the store
    at the request boundary as defense-in-depth (the schema in the key already
    prevents a cross-tenant pop; this prevents an intra-tenant stale diff too).
    """
    _before_store.__dict__.pop("data", None)


def _on_post_delete(sender: Any, instance: Any, **kwargs: Any) -> None:
    before = serialize_instance(instance)
    audit_log_on_commit(
        actor=current_actor(),
        action=AuditLog.Action.DELETE,
        resource_type=_label_for(sender),
        resource_id=instance.pk,
        before=before,
        after=None,
        request=current_request(),
        scope=audit_scope_for_instance(instance, before=before),
    )


# --------------------------------------------------------------------------- #
# Auth-flow audit (D3-D-3). Login + OTP events carry no model, so they are
# audited by listening to the published auth signals (apps.auth.signals). These
# fire synchronously from apps.auth.services with flat primitive kwargs; the row
# is written immediately (the signal itself already guards the success path).
# Logout and refresh-reuse have NO signal — those audit_log() calls are added
# directly in apps/auth/services.py by the orchestrator (see integration_needed).
# Raw IP/User-Agent are intentionally retained here as incident-response evidence
# under organization-only audit visibility and the normal retention policy. Login
# names, phone numbers, and email addresses are never retained in plaintext.
# --------------------------------------------------------------------------- #


def _resolve_actor(user_id: int | None) -> Any:
    if not user_id:
        return None
    try:
        user_model = apps.get_model("users", "User")
    except LookupError:  # pragma: no cover - users always present
        return None
    return user_model.objects.filter(pk=user_id).first()


@receiver(login_succeeded, dispatch_uid="audit.login_succeeded")
def on_login_succeeded(
    sender,
    *,
    username="",
    user_id=None,
    ip="",
    user_agent="",
    principal_kind="",
    principal_id=None,
    schema_name="",
    **kwargs,
):
    actor = _resolve_actor(user_id)
    actor_principal = None
    if (
        actor is not None
        and principal_kind in {"student", "teacher", "parent", "staff"}
        and isinstance(principal_id, int)
        and not isinstance(principal_id, bool)
        and principal_id > 0
    ):
        actor_principal = RolePrincipal(
            kind=principal_kind,
            principal_id=principal_id,
            user_id=actor.pk,
        )
    elif actor is not None:
        from django_tenants.utils import get_public_schema_name

        if schema_name == get_public_schema_name():
            actor_principal = RolePrincipal(kind="user", principal_id=actor.pk, user_id=actor.pk)
    audit_log(
        actor=actor,
        actor_principal=actor_principal,
        action=AuditLog.Action.LOGIN,
        resource_type="users.User",
        resource_id=user_id or "",
        ip=ip or None,
        user_agent=user_agent,
    )


@receiver(login_failed, dispatch_uid="audit.login_failed")
def on_login_failed(sender, *, username="", ip="", user_agent="", reason="", **kwargs):
    audit_log(
        actor=None,
        action=AuditLog.Action.LOGIN_FAILED,
        resource_type="users.User",
        after={
            "identifier_ref": private_fingerprint(username, namespace="auth-identifier"),
            "reason": reason,
        },
        ip=ip or None,
        user_agent=user_agent,
    )


@receiver(otp_requested, dispatch_uid="audit.otp_requested")
def on_otp_requested(sender, *, identifier="", purpose="", ip="", user_agent="", **kwargs):
    audit_log(
        actor=None,
        action=AuditLog.Action.OTP_REQUEST,
        resource_type="auth.OTP",
        after={
            "identifier_ref": private_fingerprint(identifier, namespace="auth-identifier"),
            "purpose": purpose,
        },
        ip=ip or None,
        user_agent=user_agent,
    )


@receiver(otp_verified, dispatch_uid="audit.otp_verified")
def on_otp_verified(sender, *, identifier="", purpose="", ip="", user_agent="", **kwargs):
    audit_log(
        actor=None,
        action=AuditLog.Action.OTP_VERIFY,
        resource_type="auth.OTP",
        after={
            "identifier_ref": private_fingerprint(identifier, namespace="auth-identifier"),
            "purpose": purpose,
        },
        ip=ip or None,
        user_agent=user_agent,
    )


@receiver(otp_failed, dispatch_uid="audit.otp_failed")
def on_otp_failed(sender, *, identifier="", ip="", user_agent="", reason="", **kwargs):
    audit_log(
        actor=None,
        action=AuditLog.Action.OTP_VERIFY,
        resource_type="auth.OTP",
        after={
            "identifier_ref": private_fingerprint(identifier, namespace="auth-identifier"),
            "reason": reason,
            "outcome": "failed",
        },
        ip=ip or None,
        user_agent=user_agent,
    )


def connect_audit_receivers() -> list[str]:
    """Wire post_save/post_delete/pre_save for every resolvable audited model.

    Returns the list of connected model labels (used by tests). Silently skips
    a model whose app/migration has not landed yet (LookupError) — siblings
    build the same day and a missing model must never crash `ready()`.
    """
    connected: list[str] = []
    for app_label, model_name in AUDITED_MODELS:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            logger.info("audit: model %s.%s not available yet; skipping", app_label, model_name)
            continue
        uid_suffix = f"{app_label}.{model_name}"
        pre_save.connect(_on_pre_save, sender=model, dispatch_uid=f"{_PRE_UID}.{uid_suffix}")
        post_save.connect(_on_post_save, sender=model, dispatch_uid=f"{_POST_UID}.{uid_suffix}")
        post_delete.connect(_on_post_delete, sender=model, dispatch_uid=f"{_DEL_UID}.{uid_suffix}")
        connected.append(f"{app_label}.{model_name}")
    return connected
