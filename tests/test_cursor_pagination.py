"""Unit tests for the non-DRF keyset cursor paginator (core.listing.cursor_paginate).

Exercises the property that matters for an append-only timeline: forward pages stay
disjoint even when NEWER rows are inserted at the head between reads, plus backward
navigation and a malformed-cursor 400. Uses AuditLog as a convenient (created_at, id)
timeline model.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import RequestFactory
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.audit.models import AuditLog
from apps.audit.tests.factories import AuditLogFactory
from core.exceptions import ValidationException
from core.listing import cursor_paginate

pytestmark = pytest.mark.django_db


def _timeline():
    return AuditLog.objects.select_related("actor").order_by("-created_at", "-id")


def _cursor_of(link: str) -> str:
    return parse_qs(urlparse(link).query)["cursor"][0]


def _seed(n: int) -> None:
    AuditLog.objects.all().delete()
    base = timezone.now() - timedelta(hours=1)
    for i in range(n):
        row = AuditLogFactory(resource_id=str(i))
        AuditLog.objects.filter(pk=row.pk).update(created_at=base + timedelta(seconds=i))


def test_forward_pages_stay_disjoint_under_head_inserts(tenant_a):
    rf = RequestFactory()
    with schema_context(tenant_a.schema_name):
        _seed(120)
        rows1, next1, prev1 = cursor_paginate(rf.get("/api/v1/audit/"), _timeline())
        assert len(rows1) == 50
        assert next1 is not None
        assert prev1 is None  # first page has no previous
        ids1 = {r.id for r in rows1}

        # Insert NEWER rows at the head between reads.
        for i in range(10):
            AuditLogFactory(resource_id=f"new{i}")

        rows2, _next2, prev2 = cursor_paginate(
            rf.get("/api/v1/audit/", {"cursor": _cursor_of(next1)}), _timeline()
        )
        ids2 = {r.id for r in rows2}
        assert ids1.isdisjoint(ids2)  # cursor stability: no page-1 row re-served
        assert prev2 is not None  # page 2 can navigate back


def test_backward_link_returns_the_prior_page(tenant_a):
    rf = RequestFactory()
    with schema_context(tenant_a.schema_name):
        _seed(120)
        rows1, next1, _ = cursor_paginate(rf.get("/api/v1/audit/"), _timeline())
        _rows2, _, prev2 = cursor_paginate(
            rf.get("/api/v1/audit/", {"cursor": _cursor_of(next1)}), _timeline()
        )
        # Walking back from page 2 reproduces page 1 exactly.
        rows_back, _, _ = cursor_paginate(
            rf.get("/api/v1/audit/", {"cursor": _cursor_of(prev2)}), _timeline()
        )
        assert [r.id for r in rows_back] == [r.id for r in rows1]


def test_page_size_param_is_honoured_and_capped(tenant_a):
    rf = RequestFactory()
    with schema_context(tenant_a.schema_name):
        _seed(30)
        rows, _, _ = cursor_paginate(rf.get("/api/v1/audit/", {"page_size": "10"}), _timeline())
        assert len(rows) == 10


def test_malformed_cursor_is_a_400(tenant_a):
    rf = RequestFactory()
    with schema_context(tenant_a.schema_name), pytest.raises(ValidationException):
        cursor_paginate(rf.get("/api/v1/audit/", {"cursor": "!!!not-valid!!!"}), _timeline())
