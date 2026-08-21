"""Strict, scope-preserving filters for the teacher directory.

These filters are applied to the already authorization-scoped queryset and before
pagination.  Compensation is deliberately handled here instead of by the generic
listing helper: even filtering by a hidden salary classification is sensitive.
"""

from __future__ import annotations

import re
from datetime import date

from django.db.models import QuerySet
from django.http import HttpRequest

from apps.teachers.models import TeacherProfile
from core.exceptions import PermissionException, ValidationException
from core.http import parse_bool

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def apply_teacher_directory_filters(
    request: HttpRequest,
    queryset: QuerySet[TeacherProfile],
    *,
    can_view_compensation: bool,
) -> QuerySet[TeacherProfile]:
    """Apply the CEO-directory filters without widening the caller's scope."""
    active_raw = _optional_value(request, "is_active")
    if active_raw is not None:
        try:
            is_active = parse_bool(active_raw, "is_active")
        except ValidationException:
            raise _filter_error("is_active") from None
        queryset = queryset.filter(is_active=is_active)

    subject = _optional_value(request, "subject")
    if subject is not None:
        if "\x00" in subject or not subject.strip():
            raise _filter_error("subject")
        # ``subjects`` is a JSON array.  ``contains=[value]`` means an exact array
        # member, not an ambiguous prefix/substring match.
        queryset = queryset.filter(subjects__contains=[subject])

    salary_type = _optional_value(request, "salary_type")
    if salary_type is not None:
        # Check access before checking the enum.  Otherwise the different 400/200
        # responses become an oracle for hidden compensation classifications.
        if not can_view_compensation:
            raise PermissionException(
                "You do not have permission to perform this action.",
                code="forbidden",
            )
        if salary_type not in TeacherProfile.SalaryType.values:
            raise _filter_error("salary_type")
        queryset = queryset.filter(salary_type=salary_type)

    hired_after = _date_filter(request, "hired_after")
    hired_before = _date_filter(request, "hired_before")
    if hired_after is not None and hired_before is not None and hired_after > hired_before:
        raise _filter_error("hired_before", "Must be on or after hired_after.")
    if hired_after is not None:
        queryset = queryset.filter(hire_date__gte=hired_after)
    if hired_before is not None:
        queryset = queryset.filter(hire_date__lte=hired_before)

    return queryset


def _optional_value(request: HttpRequest, field: str) -> str | None:
    value = request.GET.get(field)
    return None if value in (None, "") else value


def _date_filter(request: HttpRequest, field: str) -> date | None:
    raw = _optional_value(request, field)
    if raw is None:
        return None
    if _ISO_DATE_RE.fullmatch(raw) is None:
        raise _filter_error(field)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise _filter_error(field) from None


def _filter_error(field: str, message: str = "Invalid value.") -> ValidationException:
    return ValidationException(
        f"Invalid value for filter '{field}'.",
        code="validation_error",
        fields={field: [message]},
    )
