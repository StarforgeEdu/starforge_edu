from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.utils import timezone

from core.celery_base import SchemaHeaderTask
from core.exceptions import ServiceUnavailableException
from core.timezones import organization_timezone_context


def test_organization_timezone_context_activates_and_restores(monkeypatch):
    monkeypatch.setattr("core.timezones.current_schema", lambda: "tenant_alpha")
    monkeypatch.setattr(
        "apps.org.selectors.get_center_settings",
        lambda: SimpleNamespace(organization_timezone="America/New_York"),
    )
    before = timezone.get_current_timezone_name()

    with organization_timezone_context():
        assert timezone.get_current_timezone_name() == "America/New_York"

    assert timezone.get_current_timezone_name() == before


def test_public_schema_does_not_read_tenant_settings(monkeypatch):
    monkeypatch.setattr("core.timezones.current_schema", lambda: "public")

    def unexpected_read():
        raise AssertionError("public requests must not read tenant settings")

    monkeypatch.setattr("apps.org.selectors.get_center_settings", unexpected_read)
    with organization_timezone_context():
        assert timezone.get_current_timezone_name()


def test_invalid_persisted_timezone_fails_closed(monkeypatch):
    monkeypatch.setattr("core.timezones.current_schema", lambda: "tenant_alpha")
    monkeypatch.setattr(
        "apps.org.selectors.get_center_settings",
        lambda: SimpleNamespace(organization_timezone="Mars/Olympus_Mons"),
    )

    with pytest.raises(ServiceUnavailableException) as exc_info, organization_timezone_context():
        pass

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "configuration_unavailable"


def test_tenant_task_wraps_body_in_organization_timezone(monkeypatch):
    events: list[str] = []

    @contextmanager
    def timezone_context():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    monkeypatch.setattr("core.timezones.organization_timezone_context", timezone_context)

    def invoke_task(_task, *args, **kwargs):
        events.append("run")
        return "done"

    monkeypatch.setattr("tenant_schemas_celery.task.TenantTask.__call__", invoke_task)

    assert SchemaHeaderTask()() == "done"
    assert events == ["enter", "run", "exit"]
