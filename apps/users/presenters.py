"""User presenters — plain dict mappers for the layered (off-DRF) views, replacing
the DRF read serializers. Reused by other domains that embed a compact person view."""

from __future__ import annotations

from typing import Any


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
    """Was RoleMembershipSerializer."""
    return {
        "id": rm.id,
        "role": rm.role,
        "branch": rm.branch_id,
        "department": rm.department_id,
        "granted_at": _iso(rm.granted_at),
    }


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
        "role_memberships": [
            role_membership_to_dict(rm) for rm in user.role_memberships.all() if rm.revoked_at is None
        ],
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


def role_account_to_dict(kind: str, account: Any) -> dict[str, Any]:
    """Current-account payload for a role-native session; never exposes its bridge."""
    payload: dict[str, Any] = {
        "id": account.id,
        "account_type": kind,
        "username": account.username,
        "phone": account.phone,
        "email": account.email,
        "first_name": account.first_name,
        "last_name": account.last_name,
        "middle_name": account.middle_name,
        "full_name": account.get_full_name(),
        "birthdate": _iso(account.birthdate),
        "gender": account.gender,
        "is_active": account.is_active,
        "must_change_password": account.must_change_password,
        "last_login_at": _iso(account.last_login_at),
        "role_memberships": [
            role_membership_to_dict(rm) for rm in account.user.role_memberships.all() if rm.revoked_at is None
        ],
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
