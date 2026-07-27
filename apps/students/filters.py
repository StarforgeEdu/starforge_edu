"""Rich student-list filters (FEATURE_BACKLOG F2-3).

All inputs are typed by django-filter, so garbage (`?age_min=abc`) lands as a
400 validation error rather than a 500.
"""

from __future__ import annotations

import django_filters
from dateutil.relativedelta import relativedelta
from django.db.models import Q
from django.utils import timezone

from apps.students.models import StudentProfile


class StudentFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name="status")
    branch = django_filters.NumberFilter(field_name="branch_id")
    cohort = django_filters.NumberFilter(field_name="current_cohort_id")
    # with/without a group
    has_cohort = django_filters.BooleanFilter(method="_filter_has_cohort")
    level = django_filters.CharFilter(field_name="academic_level", lookup_expr="iexact")
    gender = django_filters.CharFilter(field_name="user__gender")
    location = django_filters.CharFilter(field_name="location", lookup_expr="icontains")
    previous_school = django_filters.CharFilter(field_name="previous_school", lookup_expr="icontains")
    blocked = django_filters.BooleanFilter(method="_filter_blocked")
    # students taught by a given teacher (via their current cohort's primary/co teachers)
    teacher = django_filters.NumberFilter(method="_filter_teacher")
    joined_after = django_filters.DateFilter(field_name="enrollment_date", lookup_expr="gte")
    joined_before = django_filters.DateFilter(field_name="enrollment_date", lookup_expr="lte")
    age_min = django_filters.NumberFilter(method="_filter_age_min")
    age_max = django_filters.NumberFilter(method="_filter_age_max")

    class Meta:
        model = StudentProfile
        fields: list[str] = []

    def _filter_has_cohort(self, qs, name, value):
        return qs.filter(current_cohort__isnull=not value)

    def _filter_blocked(self, qs, name, value):
        return qs.filter(blocked_at__isnull=not value)

    def _filter_teacher(self, qs, name, value):
        return qs.filter(
            Q(current_cohort__primary_teacher__user_id=value)
            | Q(current_cohort__co_teachers__teacher__user_id=value)
        ).distinct()

    def _filter_age_min(self, qs, name, value):
        # age >= value  <=>  born on or before (today minus `value` years)
        cutoff = timezone.localdate() - relativedelta(years=int(value))
        return qs.filter(birthdate__lte=cutoff)

    def _filter_age_max(self, qs, name, value):
        # age <= value  <=>  born after (today minus `value + 1` years)
        cutoff = timezone.localdate() - relativedelta(years=int(value) + 1)
        return qs.filter(birthdate__gt=cutoff)
