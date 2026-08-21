from __future__ import annotations

import pytest

from apps.notifications.models import Channel
from core.permissions import Role

pytestmark = pytest.mark.django_db

THREADS = "/api/v1/messaging/threads/"


def test_participant_mute_is_private_and_suppresses_external_alerts(
    tenant_a,
    as_role,
    monkeypatch,
):
    teacher_client, _teacher = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    outsider_client, _outsider = as_role(Role.TEACHER)
    created = teacher_client.post(
        THREADS,
        {"participant_ids": [student.pk]},
        format="json",
    )
    assert created.status_code == 201, created.content
    thread_id = created.json()["data"]["id"]
    preference_url = f"{THREADS}{thread_id}/preferences/"

    muted = student_client.patch(
        preference_url,
        {"notifications_muted": True},
        format="json",
    )

    assert muted.status_code == 200, muted.content
    assert muted.json()["data"] == {"notifications_muted": True}
    student_thread = next(row for row in student_client.get(THREADS).json()["data"] if row["id"] == thread_id)
    teacher_thread = next(row for row in teacher_client.get(THREADS).json()["data"] if row["id"] == thread_id)
    assert student_thread["notifications_muted"] is True
    assert teacher_thread["notifications_muted"] is False
    assert (
        outsider_client.patch(
            preference_url,
            {"notifications_muted": True},
            format="json",
        ).status_code
        == 404
    )

    dispatched = []
    monkeypatch.setattr(
        "apps.notifications.services.dispatch",
        lambda **kwargs: dispatched.append(kwargs),
    )
    message = teacher_client.post(
        f"{THREADS}{thread_id}/messages/",
        {"body": "Private lesson update"},
        format="json",
    )

    assert message.status_code == 201, message.content
    assert len(dispatched) == 1
    assert dispatched[0]["recipient_id"] == student.pk
    assert dispatched[0]["channels"] == [Channel.IN_APP]
    # The provider payload remains pointer-only; lock screens never receive the
    # actual message text even when the thread is unmuted later.
    assert "Private lesson update" not in str(dispatched[0]["context"])


def test_archive_and_delete_are_private_conversation_actions(tenant_a, as_role):
    teacher_client, _teacher = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    created = teacher_client.post(
        THREADS,
        {"participant_ids": [student.pk], "subject": "Lesson planning"},
        format="json",
    )
    assert created.status_code == 201, created.content
    thread_id = created.json()["data"]["id"]
    preference_url = f"{THREADS}{thread_id}/preferences/"

    archived = student_client.patch(
        preference_url,
        {"archived": True},
        format="json",
    )

    assert archived.status_code == 200, archived.content
    assert archived.json()["data"] == {"archived": True}
    student_thread = next(row for row in student_client.get(THREADS).json()["data"] if row["id"] == thread_id)
    teacher_thread = next(row for row in teacher_client.get(THREADS).json()["data"] if row["id"] == thread_id)
    assert student_thread["archived"] is True
    assert teacher_thread["archived"] is False

    deleted = student_client.delete(f"{THREADS}{thread_id}/")

    assert deleted.status_code == 204, deleted.content
    assert all(row["id"] != thread_id for row in student_client.get(THREADS).json()["data"])
    assert any(row["id"] == thread_id for row in teacher_client.get(THREADS).json()["data"])
    assert student_client.get(f"{THREADS}{thread_id}/").status_code == 404

    incoming = teacher_client.post(
        f"{THREADS}{thread_id}/messages/",
        {"body": "The conversation is active again."},
        format="json",
    )

    assert incoming.status_code == 201, incoming.content
    restored = next(row for row in student_client.get(THREADS).json()["data"] if row["id"] == thread_id)
    assert restored["archived"] is False


@pytest.mark.parametrize("payload", [{}, {"notifications_muted": "yes"}, {"notifications_muted": None}])
def test_mute_rejects_missing_or_non_boolean_values(tenant_a, as_role, payload):
    teacher_client, _teacher = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    created = teacher_client.post(
        THREADS,
        {"participant_ids": [student.pk]},
        format="json",
    )
    assert created.status_code == 201

    response = student_client.patch(
        f"{THREADS}{created.json()['data']['id']}/preferences/",
        payload,
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
