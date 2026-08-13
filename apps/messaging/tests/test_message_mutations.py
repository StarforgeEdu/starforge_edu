from __future__ import annotations

from urllib.parse import quote

import pytest
from django.db import DatabaseError, transaction
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

THREADS = "/api/v1/messaging/threads/"


def _open_thread(as_role):
    teacher_client, teacher = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    created = teacher_client.post(
        THREADS,
        {
            "participant_ids": [student.pk],
            "subject": "Mutation contract",
            "first_body": "Original body",
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    thread_id = created.json()["data"]["id"]
    first = teacher_client.get(f"{THREADS}{thread_id}/messages/")
    assert first.status_code == 200, first.content
    message = first.json()["data"][0]
    return teacher_client, teacher, student_client, student, thread_id, message


def test_author_edit_and_delete_retain_revisions_and_emit_realtime_events(tenant_a, as_role):
    from apps.messaging.models import Message, MessageRevision

    teacher_client, _teacher, student_client, _student, thread_id, message = _open_thread(as_role)
    outsider_client, _outsider = as_role(Role.TEACHER)
    url = f"/api/v1/messaging/messages/{message['id']}/"

    denied = student_client.patch(url, {"body": "Not mine"}, format="json")
    assert denied.status_code == 403
    assert denied.json()["code"] == "message_not_owned"
    assert outsider_client.patch(url, {"body": "Invisible"}, format="json").status_code == 404

    edited = teacher_client.patch(
        url,
        {"body": "Corrected body", "expected_version": 1},
        format="json",
    )
    assert edited.status_code == 200, edited.content
    edited_data = edited.json()["data"]
    assert edited_data["body"] == "Corrected body"
    assert edited_data["version"] == 2
    assert edited_data["is_deleted"] is False
    assert edited_data["reactions"] == []
    conflict = teacher_client.patch(
        url,
        {"body": "Stale edit", "expected_version": 1},
        format="json",
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "message_version_conflict"

    reacted = student_client.post(f"{url}reactions/", {"emoji": "🔥"}, format="json")
    assert reacted.status_code == 200, reacted.content
    deleted = teacher_client.delete(url)
    assert deleted.status_code == 204
    assert teacher_client.delete(url).status_code == 204
    tombstone = student_client.get(f"{THREADS}{thread_id}/messages/").json()["data"][0]
    assert tombstone["body"] == ""
    assert tombstone["attachments"] == []
    assert tombstone["version"] == 3
    assert tombstone["is_deleted"] is True
    assert tombstone["deleted_at"] is not None
    cannot_edit = teacher_client.patch(url, {"body": "Restore"}, format="json")
    assert cannot_edit.status_code == 409
    assert cannot_edit.json()["code"] == "message_deleted"

    events = student_client.get(f"{THREADS}{thread_id}/events/", {"after": 0}).json()["data"]
    assert [(row["sequence"], row["kind"]) for row in events["events"]] == [
        (1, "message.created"),
        (2, "message.updated"),
        (3, "reaction.added"),
        (4, "message.deleted"),
    ]
    with schema_context(tenant_a.schema_name):
        stored = Message.objects.get(pk=message["id"])
        assert stored.body == "Corrected body"
        assert stored.deleted_at is not None
        assert not stored.reactions.filter(removed_at__isnull=True).exists()
        assert list(
            MessageRevision.objects.filter(message=stored).values_list("version", "kind", "previous_body")
        ) == [
            (2, "edited", "Original body"),
            (3, "deleted", "Corrected body"),
        ]


def test_reactions_are_exact_principal_idempotent_and_recoverable(tenant_a, as_role):
    from apps.messaging.models import MessageReaction

    teacher_client, _teacher, student_client, _student, thread_id, message = _open_thread(as_role)
    outsider_client, _outsider = as_role(Role.TEACHER)
    url = f"/api/v1/messaging/messages/{message['id']}/reactions/"

    invalid = student_client.post(url, {"emoji": "hello"}, format="json")
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_reaction"
    assert outsider_client.post(url, {"emoji": "👍"}, format="json").status_code == 404

    first = student_client.post(url, {"emoji": "👍"}, format="json")
    assert first.status_code == 200, first.content
    assert first.json()["data"]["reactions"] == [{"emoji": "👍", "count": 1, "reacted_by_me": True}]
    repeated = student_client.post(url, {"emoji": "👍"}, format="json")
    assert repeated.status_code == 200
    assert repeated.json()["data"]["reactions"] == first.json()["data"]["reactions"]

    second_actor = teacher_client.post(url, {"emoji": "👍"}, format="json")
    assert second_actor.status_code == 200
    assert second_actor.json()["data"]["reactions"] == [{"emoji": "👍", "count": 2, "reacted_by_me": True}]
    student_view = student_client.get(f"{THREADS}{thread_id}/messages/").json()["data"][0]
    assert student_view["reactions"] == [{"emoji": "👍", "count": 2, "reacted_by_me": True}]

    remove_url = f"{url}{quote('👍')}/"
    assert student_client.delete(remove_url).status_code == 204
    assert student_client.delete(remove_url).status_code == 204
    after = student_client.get(f"{THREADS}{thread_id}/messages/").json()["data"][0]
    assert after["reactions"] == [{"emoji": "👍", "count": 1, "reacted_by_me": False}]

    events = student_client.get(f"{THREADS}{thread_id}/events/", {"after": 0}).json()["data"]
    assert [row["kind"] for row in events["events"]] == [
        "message.created",
        "reaction.added",
        "reaction.added",
        "reaction.removed",
    ]
    with schema_context(tenant_a.schema_name):
        rows = list(MessageReaction.objects.filter(message_id=message["id"]).order_by("id"))
        assert len(rows) == 2
        assert sum(row.removed_at is None for row in rows) == 1
        with pytest.raises(DatabaseError), transaction.atomic():
            MessageReaction.objects.filter(pk=rows[0].pk).delete()


def test_database_rejects_silent_message_rewrites_without_revision(tenant_a, as_role):
    from apps.messaging.models import Message

    _teacher_client, _teacher, _student_client, _student, _thread_id, message = _open_thread(as_role)
    with schema_context(tenant_a.schema_name):
        with pytest.raises(DatabaseError), transaction.atomic():
            Message.objects.filter(pk=message["id"]).update(body="silent rewrite", version=2)
        stored = Message.objects.get(pk=message["id"])
        assert stored.body == "Original body"
        assert stored.version == 1


def test_message_mutation_openapi_contracts_are_explicit_and_realtime_aware():
    from core.openapi import build_schema

    schema = build_schema("config.urls")
    detail = schema["paths"]["/api/v1/messaging/messages/{pk}/"]
    assert {method for method in detail if method in {"get", "patch", "delete"}} == {
        "patch",
        "delete",
    }
    reactions = schema["paths"]["/api/v1/messaging/messages/{pk}/reactions/"]
    assert {method for method in reactions if method in {"get", "post", "delete"}} == {"post"}
    reaction_detail = schema["paths"]["/api/v1/messaging/messages/{pk}/reactions/{emoji}/"]
    assert {method for method in reaction_detail if method in {"get", "post", "delete"}} == {"delete"}
    events = schema["paths"]["/api/v1/messaging/threads/{pk}/events/"]["get"]
    event_schema = events["responses"]["200"]["content"]["application/json"]["schema"]
    kinds = event_schema["properties"]["data"]["properties"]["events"]["items"]["properties"]["kind"]["enum"]
    assert {"message.updated", "message.deleted", "reaction.added", "reaction.removed"} <= set(kinds)
