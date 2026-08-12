"""User presenters — plain dict mappers for the layered (off-DRF) views, replacing
the DRF read serializers. Reused by other domains that embed a compact person view."""

from __future__ import annotations

from typing import Any

from core.permissions import get_effective_permission_context, get_role_memberships


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def user_brief(user: Any) -> dict[str, Any]:
    """Compact read view of a person (was UserBriefSerializer)."""
    return {
        "id": user.id,
        "username": user.username,
        "phone": user.phone,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "middle_name": user.middle_name,
        "full_name": user.get_full_name(),
        "birthdate": _iso(user.birthdate),
        "gender": user.gender,
    }


def role_membership_to_dict(rm: Any) -> dict[str, Any]:
    """Permission-native membership payload; legacy role only for null rows."""
    payload = {
        "id": rm.id,
        "account_type": rm.account_type_id,
        "branch": rm.branch_id,
        "branch_name": rm.branch.name if rm.branch_id else None,
        "department": rm.department_id,
        "department_name": rm.department.name if rm.department_id else None,
        "granted_at": _iso(rm.granted_at),
    }
    if rm.account_type_id is not None:
        payload.update(
            account_type_name=rm.account_type.name,
            account_type_slug=rm.account_type.slug,
            account_kind=rm.account_type.account_kind,
        )
    else:
        payload["legacy_role"] = rm.role
    return payload


def permission_context_to_dict(request: Any) -> dict[str, Any]:
    """Authorization bootstrap for the authenticated principal.

    Names come from the exact authorization-active membership rows loaded by the
    core resolver, preventing inactive account types or revoked assignments from
    lingering in the UI scope model.
    """
    permissions, scopes = get_effective_permission_context(request)
    memberships = get_role_memberships(request)
    branch_names = {membership.branch_id: membership.branch.name for membership in memberships}
    department_names = {
        membership.department_id: membership.department.name
        for membership in memberships
        if membership.department_id is not None
    }
    return {
        "effective_permissions": list(permissions),
        "scopes": [
            {
                "branch": (
                    {
                        "id": scope.branch_id,
                        "name": branch_names.get(scope.branch_id),
                    }
                    if scope.branch_id is not None
                    else None
                ),
                "department": (
                    {
                        "id": scope.department_id,
                        "name": department_names.get(scope.department_id),
                    }
                    if scope.department_id is not None
                    else None
                ),
                "effective_permissions": list(scope.permissions),
            }
            for scope in scopes
        ],
    }


def _active_memberships(user: Any) -> list[Any]:
    return [
        membership
        for membership in user.role_memberships.all()
        if membership.revoked_at is None
        and (membership.account_type_id is None or membership.account_type.is_active)
    ]


def user_to_dict(user: Any) -> dict[str, Any]:
    """Full user read view for /me + the directory (was UserSerializer). Includes
    the computed full name and ACTIVE-only role memberships (matches the token
    claims + permission gate, so a frontend driving UI from /me never shows stale
    roles)."""
    return {
        "id": user.id,
        "username": user.username,
        "phone": user.phone,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "middle_name": user.middle_name,
        "full_name": user.get_full_name(),
        "birthdate": _iso(user.birthdate),
        "gender": user.gender,
        "preferred_language": user.preferred_language,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "date_joined": _iso(user.date_joined),
        "last_seen_at": _iso(user.last_seen_at),
        # Filter in Python over the prefetched cache (UserRepository.query prefetches
        # role_memberships) rather than `.filter(...)`, which would bypass the cache and
        # fire a fresh query PER user — an N+1 on the directory list. `.all()` consumes the
        # prefetch (0 extra queries on the list path); on the un-prefetched /me path it is
        # one small query, same as before.
        "role_memberships": [role_membership_to_dict(rm) for rm in _active_memberships(user)],
    }


def user_directory_row_to_dict(user: Any) -> dict[str, Any]:
    """PII-minimized row for the paginated management directory.

    The list surface only needs a stable identity, display name, business contact,
    activation state, and recent activity.  Sensitive profile attributes and the
    caller-scoped membership graph remain available from the permission-scoped
    detail endpoint instead of being repeated across every list row.
    """
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.get_full_name(),
        "phone": user.phone,
        "is_active": user.is_active,
        "last_seen_at": _iso(user.last_seen_at),
    }


def device_to_dict(device: Any) -> dict[str, Any]:
    """Device read view (was DeviceSerializer) — never exposes the raw push_token."""
    return {
        "id": device.id,
        "device_id": device.device_id,
        "platform": device.platform,
        "user_agent": device.user_agent,
        "last_seen_at": _iso(device.last_seen_at),
        "created_at": _iso(device.created_at),
    }


def session_to_dict(session: Any, *, current_session_id: int) -> dict[str, Any]:
    """Privacy-minimized authenticated-session row.

    Raw user-agent, IP address, opaque key digest, and device identifiers are
    intentionally absent.  The coarse labels are sufficient for a person to
    recognize and revoke a session without becoming a fingerprinting surface.
    """

    from core.session_auth import session_idle_timeout

    user_agent = str(session.user_agent or "")
    idle_expires_at = min(
        session.expires_at,
        session.last_used_at + session_idle_timeout(),
    )
    platform = str(getattr(session, "device_platform", "") or "").lower()
    if platform not in {"web", "ios", "android"}:
        platform = "web"
    return {
        "id": session.id,
        "platform": platform,
        "device": _device_family(user_agent),
        "browser": _browser_family(user_agent),
        "created_at": _iso(session.created_at),
        "last_activity_at": _iso(session.last_used_at),
        "expires_at": _iso(session.expires_at),
        "idle_expires_at": _iso(idle_expires_at),
        "current_session": session.id == current_session_id,
        "read_only": bool(session.read_only),
    }


def _browser_family(user_agent: str) -> str:
    lowered = user_agent.lower()
    for marker, label in (
        ("edg/", "Edge"),
        ("edgios/", "Edge"),
        ("edga/", "Edge"),
        ("firefox/", "Firefox"),
        ("fxios/", "Firefox"),
        ("crios/", "Chrome"),
        ("chrome/", "Chrome"),
        ("safari/", "Safari"),
    ):
        if marker in lowered:
            return label
    return "Other"


def _device_family(user_agent: str) -> str:
    lowered = user_agent.lower()
    for marker, label in (
        ("ipad", "iPad"),
        ("iphone", "iPhone"),
        ("android", "Android"),
        ("windows", "Windows"),
        ("macintosh", "macOS"),
        ("cros", "ChromeOS"),
        ("linux", "Linux"),
    ):
        if marker in lowered:
            return label
    return "Unknown"


def role_account_to_dict(kind: str, account: Any, *, memberships: list[Any] | None = None) -> dict[str, Any]:
    """Current-account payload for a role-native session.

    ``id`` remains the role-profile id. ``messaging_user_id`` explicitly exposes
    the current account's bridge id so clients cannot accidentally pass a profile
    id to messaging's legacy ``participant_ids`` contract.
    """
    if memberships is None:
        legacy_roles = {
            "student": {"student"},
            "teacher": {"teacher"},
            "parent": {"parent"},
            "staff": {
                "director",
                "head_of_dept",
                "accountant",
                "cashier",
                "librarian",
                "security",
                "it",
                "registrar",
                "support",
            },
        }.get(kind, set())
        memberships = [
            membership
            for membership in _active_memberships(account.user)
            if (membership.account_type_id is not None and membership.account_type.account_kind == kind)
            or (membership.account_type_id is None and membership.role in legacy_roles)
        ]
    payload: dict[str, Any] = {
        "id": account.id,
        "messaging_user_id": account.user_id,
        "principal_kind": kind,
        "username": account.username,
        "phone": account.phone,
        "email": account.email,
        "first_name": account.first_name,
        "last_name": account.last_name,
        "middle_name": account.middle_name,
        "full_name": account.get_full_name(),
        "birthdate": _iso(account.birthdate),
        "gender": account.gender,
        "preferred_language": account.user.preferred_language,
        "is_active": account.is_active,
        "must_change_password": account.must_change_password,
        "last_login_at": _iso(account.last_login_at),
        "role_memberships": [role_membership_to_dict(rm) for rm in memberships],
    }
    if kind == "student":
        payload.update(
            student_id=account.student_id,
            status=account.status,
            branch=account.branch_id,
            current_cohort=account.current_cohort_id,
        )
    elif kind == "teacher":
        payload.update(branch=account.branch_id, department=account.department_id)
    return payload
