"""Tenant task dispatch must fail closed before work reaches the broker."""

from __future__ import annotations

import pytest

from apps.tenancy.models import Center
from core.celery_base import SchemaHeaderTask


class _Request(dict):
    def __init__(self, *, headers=None, **values):
        super().__init__(values)
        self.headers = headers


class _ProbeTask(SchemaHeaderTask):
    name = "tests.probe_tenant_routing"

    def run(self):
        return None


def test_unknown_tenant_schema_is_rejected_without_echoing_identifier(monkeypatch):
    monkeypatch.setattr(Center.objects, "filter", lambda **_kwargs: _Exists(False))

    with pytest.raises(ValueError, match="unknown tenant schema") as caught:
        SchemaHeaderTask._assert_schema_resolvable("private-customer-schema")

    assert "private-customer-schema" not in str(caught.value)


def test_tenant_registry_failure_refuses_dispatch(monkeypatch):
    def unavailable(**_kwargs):
        raise OSError("database diagnostics that must remain chained only")

    monkeypatch.setattr(Center.objects, "filter", unavailable)

    with pytest.raises(RuntimeError, match="task dispatch was refused"):
        SchemaHeaderTask._assert_schema_resolvable("tenant_alpha")


def test_worker_refuses_execution_when_prerun_did_not_activate_expected_schema():
    with pytest.raises(RuntimeError, match="task execution was refused"):
        SchemaHeaderTask._assert_execution_schema(
            expected="tenant_alpha",
            actual="public",
        )


def test_worker_accepts_only_the_exact_activated_schema():
    SchemaHeaderTask._assert_execution_schema(
        expected="tenant_alpha",
        actual="tenant_alpha",
    )


def test_explicit_schema_cannot_be_overridden_by_conflicting_celery_header(monkeypatch):
    monkeypatch.setattr(_ProbeTask, "_assert_schema_resolvable", lambda _schema: None)
    task = _ProbeTask()

    with pytest.raises(ValueError, match="Conflicting tenant routing") as caught:
        task.apply_async(
            kwargs={"_schema_name": "tenant_expected"},
            headers={"_schema_name": "tenant_other"},
        )

    assert "tenant_expected" not in str(caught.value)
    assert "tenant_other" not in str(caught.value)


@pytest.mark.parametrize(
    ("task_request", "expected"),
    [
        (_Request(headers={"_schema_name": "tenant_amqp"}), "tenant_amqp"),
        (_Request(_schema_name="tenant_redis"), "tenant_redis"),
        (_Request(headers={"_schema_name": "tenant_header"}, _schema_name="tenant_body"), "tenant_header"),
        (_Request(), None),
        (None, None),
    ],
)
def test_worker_reads_schema_from_amqp_headers_or_redis_request_mapping(task_request, expected):
    assert SchemaHeaderTask._schema_from_request(task_request) == expected


class _Exists:
    def __init__(self, value: bool) -> None:
        self.value = value

    def exists(self) -> bool:
        return self.value
