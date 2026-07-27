"""Teacher presenters — plain dict mappers (replace TeacherReadSerializer)."""

from __future__ import annotations

from typing import Any

from apps.teachers.models import PayoutPolicy, TeacherProfile


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


def teacher_to_dict(teacher: TeacherProfile) -> dict[str, Any]:
    # Each bare FK id keeps a readable `_name` companion so a client renders the teacher
    # without a second call. `branch`/`department` are select_related on both the list
    # queryset (repository.get_queryset + selectors.list_teachers) and detail path, so
    # these add JOINs, not queries. `branch` is non-null; `department` is nullable.
    return {
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
        "salary_type": teacher.salary_type,
        "rate": str(teacher.rate) if teacher.rate is not None else None,
        "is_substitute": teacher.is_substitute,
        "created_at": teacher.created_at.isoformat(),
    }
