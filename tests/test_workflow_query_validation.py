"""Database-free validation regressions for decision-critical workflow reads."""

from __future__ import annotations

import pytest
from django.test import RequestFactory

from apps.forms.views.v1.form_views import (
    _validate_filters as validate_form_filters,
)
from apps.forms.views.v1.form_views import (
    _validate_query as validate_form_query,
)
from apps.meetings.views.v1.meeting_views import (
    _validate_filters as validate_meeting_filters,
)
from apps.meetings.views.v1.meeting_views import (
    _validate_query as validate_meeting_query,
)
from apps.tasks.views.v1.task_views import (
    _validate_task_filters as validate_task_filters,
)
from core.exceptions import ValidationException


def _request(query: str):
    return RequestFactory().get(f"/api/v1/workflow/?{query}")


def _assert_field_error(call, query: str, field: str) -> None:
    with pytest.raises(ValidationException) as captured:
        call(_request(query))
    assert captured.value.code == "validation_error"
    assert field in (captured.value.fields or {})


@pytest.mark.parametrize(
    ("call", "query", "field"),
    [
        (validate_form_filters, "unexpected=1", "unexpected"),
        (validate_meeting_filters, "unexpected=1", "unexpected"),
        (validate_task_filters, "unexpected=1", "unexpected"),
        (validate_form_filters, "ordering=--created_at", "ordering"),
        (validate_meeting_filters, "ordering=--starts_at", "ordering"),
        (validate_task_filters, "ordering=--priority", "ordering"),
        (validate_form_filters, f"search={'x' * 201}", "search"),
        (validate_task_filters, f"search={'x' * 201}", "search"),
        (validate_task_filters, "assignee_kind=teacher", "assignee_principal_id"),
        (validate_task_filters, "assignee_principal_id=1", "assignee_kind"),
        (validate_task_filters, "assignee_kind=student&assignee_principal_id=1", "assignee_kind"),
    ],
)
def test_workflow_collection_filters_fail_closed(call, query, field):
    _assert_field_error(call, query, field)


@pytest.mark.parametrize(
    ("call", "query", "field"),
    [
        (lambda request: validate_form_query(request, allowed={"page", "page_size"}), "page=0", "page"),
        (
            lambda request: validate_meeting_query(request, allowed={"page", "page_size"}),
            "page=not-an-integer",
            "page",
        ),
        (
            lambda request: validate_form_query(request, allowed={"page", "page_size"}),
            "page_size=101",
            "page_size",
        ),
        (
            lambda request: validate_meeting_query(request, allowed={"page", "page_size"}),
            "page=1&page=2",
            "page",
        ),
    ],
)
def test_workflow_pagination_is_strict_and_single_valued(call, query, field):
    _assert_field_error(call, query, field)
