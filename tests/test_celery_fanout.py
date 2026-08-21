"""Per-tenant Celery fan-out routing contract (D2-A-7 + the duplicate fan-out
finding).

Test settings replace tenant_schemas_celery.TenantTask with the plain Celery
Task (CELERY_TASK_CLS), so the production tenant-routing hop — a dispatcher
enqueuing one per-schema task per ACTIVE Center with ``_schema_name`` set — is
never exercised by the periodic-task tests (they call the service bodies directly
inside an explicit schema_context). This pins that contract without a live worker:
monkeypatch the per-schema task's ``.delay`` and assert the dispatcher passes
``_schema_name`` for every active Center and skips inactive ones.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


def _run_dispatcher_capturing_schema_names(dispatcher, per_schema_task, monkeypatch):
    """Call ``dispatcher`` with ``per_schema_task.delay`` monkeypatched to record
    the ``_schema_name`` kwarg of every enqueue. Returns (return_value, [schemas])."""
    captured: list[str | None] = []

    def _fake_delay(*args, **kwargs):
        captured.append(kwargs.get("_schema_name"))
        return None

    monkeypatch.setattr(per_schema_task, "delay", _fake_delay)
    result = dispatcher()
    return result, captured


def test_send_lesson_reminders_fans_out_per_active_center(tenant_a, tenant_b, monkeypatch):
    from apps.tenancy.models import Center
    from celery_tasks import schedule_tasks

    # Both fixtures are active Centers; the dispatcher must enqueue one per-schema
    # task per active Center, each with _schema_name set to that schema.
    result, captured = _run_dispatcher_capturing_schema_names(
        schedule_tasks.send_lesson_reminders,
        schedule_tasks.send_lesson_reminders_for_schema,
        monkeypatch,
    )

    active = set(Center.objects.filter(is_active=True).values_list("schema_name", flat=True))
    assert result == len(active)
    # Every enqueue carried a real schema name (never None / dropped kwarg) ...
    assert all(name is not None for name in captured)
    # ... and the set of routed schemas is exactly the active Centers.
    assert set(captured) == active
    assert tenant_a.schema_name in captured
    assert tenant_b.schema_name in captured


def test_send_lesson_reminders_skips_inactive_center(tenant_a, tenant_b, monkeypatch):
    from apps.tenancy.models import Center
    from celery_tasks import schedule_tasks

    # Deactivate tenant_b: it must be excluded from the fan-out.
    Center.objects.filter(schema_name=tenant_b.schema_name).update(is_active=False)
    try:
        result, captured = _run_dispatcher_capturing_schema_names(
            schedule_tasks.send_lesson_reminders,
            schedule_tasks.send_lesson_reminders_for_schema,
            monkeypatch,
        )
        active = set(Center.objects.filter(is_active=True).values_list("schema_name", flat=True))
        assert result == len(active)
        assert tenant_b.schema_name not in captured
        assert tenant_a.schema_name in captured
        assert set(captured) == active
    finally:
        Center.objects.filter(schema_name=tenant_b.schema_name).update(is_active=True)


@pytest.mark.parametrize(
    ("module_name", "dispatcher_name", "body_name"),
    [
        ("finance_tasks", "late_payment_reminders", "late_payment_reminders_for_schema"),
        ("finance_tasks", "refresh_fx_rates", "refresh_fx_rate_for_schema"),
        ("billing_tasks", "run_nightly_metering", "meter_center_task"),
    ],
)
def test_money_and_metering_dispatchers_isolate_each_tenant(
    tenant_a,
    tenant_b,
    monkeypatch,
    module_name,
    dispatcher_name,
    body_name,
):
    """Periodic money work must enqueue independent retryable tenant bodies."""
    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center
    from celery_tasks import billing_tasks, finance_tasks

    module = {"finance_tasks": finance_tasks, "billing_tasks": billing_tasks}[module_name]
    dispatcher = getattr(module, dispatcher_name)
    body = getattr(module, body_name)
    result, captured = _run_dispatcher_capturing_schema_names(dispatcher, body, monkeypatch)

    active = set(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .values_list("schema_name", flat=True)
    )
    assert result == len(active)
    assert set(captured) == active
    assert {tenant_a.schema_name, tenant_b.schema_name} <= active
