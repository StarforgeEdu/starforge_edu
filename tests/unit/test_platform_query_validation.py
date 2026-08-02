"""Database-free control-plane query-contract regressions."""

import pytest
from django.test import RequestFactory

from apps.tenancy.views.v1.tenancy_views import _validate_query
from core.exceptions import ValidationException


def test_platform_query_validator_accepts_only_one_value_per_declared_field():
    request = RequestFactory().get("/api/v1/platform/resolve/?slug=tenant-a")

    _validate_query(request, allowed={"slug"})


@pytest.mark.parametrize(
    ("query", "field", "message"),
    [
        ("slug=tenant-a&redirect=https://example.test", "redirect", "not supported"),
        ("slug=tenant-a&slug=tenant-b", "slug", "only once"),
    ],
)
def test_platform_query_validator_rejects_unknown_and_repeated_values(query, field, message):
    request = RequestFactory().get(f"/api/v1/platform/resolve/?{query}")

    with pytest.raises(ValidationException) as caught:
        _validate_query(request, allowed={"slug"})

    assert field in caught.value.fields
    assert message in str(caught.value.fields[field][0])
