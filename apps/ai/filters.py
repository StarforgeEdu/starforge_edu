"""django-filter FilterSet for the AI request log (DoD #5)."""

from __future__ import annotations

import django_filters

from apps.ai.models import AIRequest


class AIRequestFilter(django_filters.FilterSet):
    created_after = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = AIRequest
        fields = ("feature", "status", "created_after", "created_before")
