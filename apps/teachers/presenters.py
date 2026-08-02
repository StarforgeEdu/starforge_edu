"""Teacher presenters — plain dict mappers (replace TeacherReadSerializer)."""

from __future__ import annotations

from typing import Any

from apps.access.models import AccountType
from apps.teachers.models import PayoutPolicy, TeacherProfile
from core.permissions import Role


def payout_policy_to_dict(policy: PayoutPolicy) -> dict[str, Any]:
    def _d(v):
        return str(v) if v is not None else None

    return {
        "teacher": policy.teacher_id,
        "method": policy.method,
        "hourly_rate_uzs": _d(policy.hourly_rate_uzs),
        "flat_amount_uzs": _d(policy.flat_amount_uzs),
        "tuition_percent": _d(policy.tuition_percent),
        "is_active": policy.is_active,
        "updated_at": policy.updated_at.isoformat(),
    }


def teacher_to_dict(
    teacher: TeacherProfile,
    *,
    include_compensation: bool = False,
) -> dict[str, Any]:
    # Each bare FK id keeps a readable `_name` companion so a client renders the teacher
    # without a second call. `branch`/`department` are select_related on both the list
    # queryset (repository.get_queryset + selectors.list_teachers) and detail path, so
    # these add JOINs, not queries. `branch` is non-null; `department` is nullable.
    from apps.users.presenters import role_membership_to_dict

    # The bridge User can legitimately own staff/student/parent profiles too.
    # A teacher directory must never expose those unrelated memberships or a
    # teacher assignment from another branch merely because the bridge id is
    # shared. The repository prefetch applies the same account-kind boundary;
    # this projection also protects callers that pass an un-prefetched model.
    memberships = [
        membership
        for membership in teacher.user.role_memberships.all()
        if membership.revoked_at is None
        and membership.branch_id == teacher.branch_id
        and (
            (
                membership.account_type_id is not None
                and (account_type := membership.account_type) is not None
                and account_type.is_active
                and account_type.account_kind == AccountType.AccountKind.TEACHER
            )
            or (membership.account_type_id is None and membership.role == Role.TEACHER)
        )
    ]
    payload = {
        "id": teacher.id,
        "username": teacher.username,
        "is_active": teacher.is_active,
        "must_change_password": teacher.must_change_password,
        "last_login_at": teacher.last_login_at.isoformat() if teacher.last_login_at else None,
        # Identity owned by the teacher model (role-native auth); `user` kept for the
        # login/username reference + back-compat.
        "first_name": teacher.first_name,
        "last_name": teacher.last_name,
        "middle_name": teacher.middle_name,
        "full_name": teacher.get_full_name(),
        "phone": teacher.phone,
        "email": teacher.email,
        "birthdate": teacher.birthdate.isoformat() if teacher.birthdate else None,
        "gender": teacher.gender,
        "branch": teacher.branch_id,
        "branch_name": teacher.branch.name if teacher.branch_id else None,
        "department": teacher.department_id,
        "department_name": teacher.department.name if teacher.department else None,
        "hire_date": teacher.hire_date.isoformat() if teacher.hire_date else None,
        "subjects": teacher.subjects,
        "qualifications": teacher.qualifications,
        "is_substitute": teacher.is_substitute,
        "account_type_assignments": [role_membership_to_dict(membership) for membership in memberships],
        "created_at": teacher.created_at.isoformat(),
    }
    if include_compensation:
        payload.update(
            {
                "salary_type": teacher.salary_type,
                "rate": str(teacher.rate) if teacher.rate is not None else None,
            }
        )
    return payload
