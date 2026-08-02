"""DB-free complexity and contract tests for bulk schedule notification fan-out."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _moves(count: int) -> list[dict[str, object]]:
    return [
        {
            "lesson_id": index + 1,
            "old_start": f"2026-08-{(index % 28) + 1:02d}T09:00:00+05:00",
            "moved_at": f"2026-08-01T10:00:{index:02d}.000001+05:00",
            "move_id": f"{index + 1:032x}",
        }
        for index in range(count)
    ]


def test_bulk_signal_enqueues_one_coordinator_not_lesson_recipient_product(monkeypatch):
    """Fifty moved lessons publish one job and perform no inline dispatch/query."""
    from apps.notifications import receivers
    from apps.schedule.signals import lessons_bulk_rescheduled
    from celery_tasks import notification_tasks

    queued: list[dict] = []
    monkeypatch.setattr(
        notification_tasks.coordinate_lesson_reschedule_fanout,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )
    monkeypatch.setattr(
        receivers,
        "_cohort_member_recipients",
        lambda _cohort_id: pytest.fail("the request path resolved cohort recipients"),
    )
    monkeypatch.setattr(
        receivers.services,
        "dispatch",
        lambda **_kwargs: pytest.fail("the request path dispatched a recipient"),
    )

    lessons_bulk_rescheduled.send(
        sender=None,
        cohort_id=17,
        moves=_moves(50),
        actor_id=9,
        schema_name="tenant_alpha",
    )

    assert queued == [
        {
            "cohort_id": 17,
            "moves": _moves(50),
            "_schema_name": "tenant_alpha",
        }
    ]


def test_coordinator_streams_exact_principals_into_bounded_child_jobs(monkeypatch):
    from apps.cohorts.models import CohortMembership
    from celery_tasks import notification_tasks

    class _Rows:
        def filter(self, **_kwargs):
            return self

        def order_by(self, *_fields):
            return self

        def values_list(self, *_fields):
            return self

        def iterator(self, *, chunk_size):
            assert chunk_size == notification_tasks.LESSON_RESCHEDULE_RECIPIENTS_PER_TASK
            return iter((index + 1000, index + 1) for index in range(125))

    queued: list[dict] = []
    monkeypatch.setattr(CohortMembership, "objects", _Rows())
    monkeypatch.setattr(
        "core.utils.current_schema",
        lambda: "tenant_alpha",
    )
    monkeypatch.setattr(
        notification_tasks.dispatch_lesson_reschedule_chunk,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )

    result = notification_tasks.coordinate_lesson_reschedule_fanout.run(
        cohort_id=17,
        moves=_moves(12),
    )

    # ceil(12/5) event chunks x ceil(125/50) recipient chunks.
    assert result == {"events": 12, "recipients": 125, "tasks": 9}
    assert len(queued) == 9
    assert all(
        1 <= len(job["moves"]) <= notification_tasks.LESSON_RESCHEDULE_EVENTS_PER_TASK for job in queued
    )
    assert all(
        1 <= len(job["recipients"]) <= notification_tasks.LESSON_RESCHEDULE_RECIPIENTS_PER_TASK
        for job in queued
    )
    assert all(job["_schema_name"] == "tenant_alpha" for job in queued)
    assert all(recipient["principal_kind"] == "student" for job in queued for recipient in job["recipients"])


def test_child_preserves_exact_principals_and_each_repeated_move(monkeypatch):
    from apps.notifications import services
    from celery_tasks import notification_tasks

    moves = [
        {
            "lesson_id": 42,
            "old_start": "2026-08-01T09:00:00+05:00",
            "moved_at": "2026-08-01T10:00:00.000001+05:00",
            "move_id": "00000000000000000000000000000001",
        },
        {
            "lesson_id": 42,
            "old_start": "2026-08-08T09:00:00+05:00",
            "moved_at": "2026-08-01T10:00:00.000002+05:00",
            "move_id": "00000000000000000000000000000002",
        },
        {
            "lesson_id": 42,
            "old_start": "2026-08-01T09:00:00+05:00",
            "moved_at": "2026-08-01T10:00:00.000003+05:00",
            "move_id": "00000000000000000000000000000003",
        },
    ]
    recipients = [
        {"user_id": 101, "principal_kind": "student", "principal_id": 1},
        {"user_id": 102, "principal_kind": "student", "principal_id": 2},
    ]
    calls: list[dict] = []
    monkeypatch.setattr(
        services,
        "dispatch",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(is_deliverable=True),
    )

    result = notification_tasks.dispatch_lesson_reschedule_chunk.run(
        moves=moves,
        recipients=recipients,
    )

    assert result == {"attempted": 6, "deliverable": 6}
    assert len({call["dedupe_key"] for call in calls}) == 6
    assert {
        (call["recipient_id"], call["recipient_principal_kind"], call["recipient_principal_id"])
        for call in calls
    } == {(101, "student", 1), (102, "student", 2)}
    # Old start repeats on move three, but stable per-operation IDs keep it distinct.
    assert len({call["dedupe_key"].rsplit(":", 1)[0] for call in calls}) == 3


def test_child_rejects_work_units_above_the_bound():
    from celery_tasks import notification_tasks

    with pytest.raises(ValueError, match="between 1 and"):
        notification_tasks.dispatch_lesson_reschedule_chunk.run(
            moves=_moves(notification_tasks.LESSON_RESCHEDULE_EVENTS_PER_TASK + 1),
            recipients=[
                {"user_id": 101, "principal_kind": "student", "principal_id": 1},
            ],
        )
