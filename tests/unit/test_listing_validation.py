from __future__ import annotations

import base64

import pytest
from django.test import RequestFactory

from core.exceptions import ValidationException
from core.listing import (
    MAX_PAGE_SIZE,
    MAX_SEARCH_LENGTH,
    _decode_cursor,
    apply_filters,
    paginate_sequence,
)


@pytest.mark.parametrize(
    "query",
    [
        {"page": "not-a-number"},
        {"page": "0"},
        {"page": "-1"},
        {"page": ""},
        {"page_size": "not-a-number"},
        {"page_size": "0"},
        {"page_size": ""},
        {"page_size": str(MAX_PAGE_SIZE + 1)},
    ],
)
def test_offset_pagination_rejects_invalid_or_unsupported_values(query):
    request = RequestFactory().get("/api/v1/example/", query)
    with pytest.raises(ValidationException) as captured:
        paginate_sequence(request, [1, 2, 3])
    assert captured.value.code == "validation_error"
    assert set(captured.value.fields or {}) & {"page", "page_size"}


def test_offset_pagination_keeps_large_valid_page_safe_and_empty():
    request = RequestFactory().get("/api/v1/example/", {"page": "999999999999999999"})
    items, total, page, size = paginate_sequence(request, [1, 2, 3])
    assert items == []
    assert total == 3
    assert page == 999999999999999999
    assert size == 25


def _cursor_token(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode()).decode()


@pytest.mark.parametrize(
    "token",
    [
        "!!!not-base64!!!",
        _cursor_token("x|2026-08-02T10:00:00+05:00|1"),
        _cursor_token("f|2026-08-02T10:00:00|1"),
        _cursor_token("f|2026-08-02T10:00:00+05:00|0"),
        _cursor_token("f|2026-08-02T10:00:00+05:00|-1"),
        _cursor_token("f|2026-08-02T10:00:00+05:00|9223372036854775808"),
    ],
)
def test_cursor_rejects_noncanonical_or_out_of_range_positions(token):
    with pytest.raises(ValidationException) as captured:
        _decode_cursor(token)
    assert captured.value.code == "validation_error"
    assert captured.value.fields == {"cursor": ["Invalid value."]}


@pytest.mark.parametrize(
    ("query_string", "field"),
    [
        ("page=1&page=2", "page"),
        ("page_size=10&page_size=20", "page_size"),
    ],
)
def test_pagination_rejects_duplicate_scalar_parameters(query_string, field):
    request = RequestFactory().get(f"/api/v1/example/?{query_string}")
    with pytest.raises(ValidationException) as captured:
        paginate_sequence(request, [1, 2, 3])
    assert captured.value.fields == {field: ["Supply this parameter once."]}


@pytest.mark.parametrize(
    ("query_string", "field"),
    [
        ("search=one&search=two", "search"),
        ("ordering=id&ordering=-id", "ordering"),
        ("status=open&status=closed", "status"),
    ],
)
def test_listing_rejects_duplicate_scalar_parameters(query_string, field):
    from apps.tasks.models import Task

    request = RequestFactory().get(f"/api/v1/example/?{query_string}")
    with pytest.raises(ValidationException) as captured:
        apply_filters(
            request,
            Task.objects.none(),
            filter_fields=("status",),
            search_fields=("title",),
            ordering_fields=("id",),
        )
    assert captured.value.fields == {field: ["Supply this parameter once."]}


def test_listing_rejects_oversized_search_before_database_work():
    from apps.tasks.models import Task

    request = RequestFactory().get(
        "/api/v1/example/",
        {"search": "x" * (MAX_SEARCH_LENGTH + 1)},
    )
    with pytest.raises(ValidationException) as captured:
        apply_filters(request, Task.objects.none(), search_fields=("title",))
    assert captured.value.fields == {"search": ["Invalid value."]}


def test_listing_rejects_invalid_model_choice_filter():
    from apps.tasks.models import Task

    request = RequestFactory().get("/api/v1/example/", {"status": "not-a-status"})
    with pytest.raises(ValidationException) as captured:
        apply_filters(request, Task.objects.none(), filter_fields=("status",))
    assert captured.value.fields == {"status": ["Invalid value."]}
