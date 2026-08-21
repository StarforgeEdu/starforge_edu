from __future__ import annotations

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.utils import schema_context

from apps.payroll.dto import PreviewFilterDTO
from apps.payroll.presenters import line_to_dict
from apps.payroll.repositories import PayrollRepository
from apps.payroll.services import preview_period, run_period

from .helpers import make_actor, make_period, make_teacher

pytestmark = pytest.mark.django_db


def test_preview_query_count_is_effectively_constant_with_teacher_count(tenant_a):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        runner = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:run"),
        )
        teachers = [make_teacher(branch=branch) for _ in range(20)]
        period = make_period(actor=runner, branch=branch)

        cache.clear()
        with CaptureQueriesContext(connection) as one_context:
            one = preview_period(
                period=period,
                filters=PreviewFilterDTO((teachers[0].pk,)),
            )
        cache.clear()
        with CaptureQueriesContext(connection) as many_context:
            many = preview_period(
                period=period,
                filters=PreviewFilterDTO(tuple(teacher.pk for teacher in teachers)),
            )

        assert one["teacher_count"] == 1
        assert many["teacher_count"] == 20
        assert len(many_context) <= len(one_context) + 1
        assert len(many_context) <= 8


def test_line_register_presenter_has_no_per_row_queries(tenant_a):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        runner = make_actor(
            branch=branch,
            permissions=("compensation:read", "compensation:run"),
        )
        teachers = [make_teacher(branch=branch) for _ in range(25)]
        period = make_period(actor=runner, branch=branch)
        period = run_period(
            period=period,
            filters=PreviewFilterDTO(tuple(teacher.pk for teacher in teachers)),
            actor=runner.user,
            principal=runner.principal,
            idempotency_key="query-count-run-0001",
        )

        with CaptureQueriesContext(connection) as context:
            payload = [line_to_dict(line) for line in PayrollRepository().lines(period=period)]

        assert len(payload) == 25
        assert len(context) == 1
