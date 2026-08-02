from __future__ import annotations

import json
from io import StringIO

import pytest
from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from config.asgi import application
from core.permissions import Role

THREADS = "/api/v1/messaging/threads/"
HOST = [(b"host", b"a.localhost")]


def _access_token(client) -> str:
    value = client._credentials["HTTP_AUTHORIZATION"]
    if isinstance(value, bytes):
        value = value.decode("ascii")
    scheme, token = value.split(" ", 1)
    assert scheme == "Bearer"
    return token


def _open_thread(as_role, *, first_body: str = "first private message"):
    teacher_client, teacher = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    response = teacher_client.post(
        THREADS,
        {
            "subject": "Realtime contract",
            "participant_ids": [student.pk],
            "first_body": first_body,
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    return teacher_client, teacher, student_client, student, response.json()["data"]


@pytest.mark.django_db
def test_event_recovery_is_ordered_pointer_only_and_read_cursor_is_idempotent(tenant_a, as_role):
    teacher_client, _teacher, student_client, _student, thread = _open_thread(as_role)
    thread_id = thread["id"]
    assert thread["realtime_cursor"] == 1
    assert thread["realtime_protocol"] == "starforge.messaging.thread.v1"

    initial = student_client.get(f"{THREADS}{thread_id}/events/", {"after": 0, "limit": 10})
    assert initial.status_code == 200, initial.content
    page = initial.json()["data"]
    assert page["high_watermark"] == 1
    assert page["next_cursor"] == 1
    assert page["has_more"] is False
    assert page["reset_required"] is False
    assert [(row["sequence"], row["kind"]) for row in page["events"]] == [(1, "message.created")]
    assert "first private message" not in initial.content.decode()
    first_message_id = page["events"][0]["message_id"]

    read = student_client.post(
        f"{THREADS}{thread_id}/read/",
        {"through_message_id": first_message_id},
        format="json",
    )
    assert read.status_code == 200, read.content
    assert read.json()["data"] == {
        "thread_id": thread_id,
        "changed": True,
        "through_message_id": first_message_id,
        "read_at": read.json()["data"]["read_at"],
        "event_cursor": 2,
    }

    repeated = student_client.post(
        f"{THREADS}{thread_id}/read/",
        {"through_message_id": first_message_id},
        format="json",
    )
    assert repeated.status_code == 200
    assert repeated.json()["data"]["changed"] is False
    assert repeated.json()["data"]["event_cursor"] is None

    second = teacher_client.post(
        f"{THREADS}{thread_id}/messages/",
        {"body": "second private message"},
        format="json",
    )
    assert second.status_code == 201, second.content
    second_message_id = second.json()["data"]["id"]
    assert second.json()["data"]["sender_principal_kind"] == "teacher"
    assert second.json()["data"]["sender_attribution_status"] == "captured"

    delta = student_client.get(f"{THREADS}{thread_id}/events/", {"after": 1})
    assert [(row["sequence"], row["kind"], row["message_id"]) for row in delta.json()["data"]["events"]] == [
        (2, "read.updated", first_message_id),
        (3, "message.created", second_message_id),
    ]
    messages = student_client.get(
        f"{THREADS}{thread_id}/messages/",
        {"after_id": first_message_id},
    )
    assert [row["id"] for row in messages.json()["data"]] == [second_message_id]

    # Advancing to an older cursor cannot accidentally mark the racing message read.
    stale = student_client.post(
        f"{THREADS}{thread_id}/read/",
        {"through_message_id": first_message_id},
        format="json",
    )
    assert stale.json()["data"]["changed"] is False
    student_thread = next(row for row in student_client.get(THREADS).json()["data"] if row["id"] == thread_id)
    assert student_thread["unread_count"] == 1


@pytest.mark.django_db
def test_event_recovery_rejects_cursor_confusion_unknown_queries_and_outsiders(tenant_a, as_role):
    teacher_client, _teacher, student_client, _student, thread = _open_thread(as_role)
    outsider_client, _outsider = as_role(Role.TEACHER)
    url = f"{THREADS}{thread['id']}/events/"

    ahead = student_client.get(url, {"after": thread["realtime_cursor"] + 1})
    assert ahead.status_code == 400
    assert ahead.json()["code"] == "invalid_event_cursor"
    assert student_client.get(url, {"after": "-1"}).status_code == 400
    assert student_client.get(url, {"after": "9223372036854775808"}).status_code == 400
    assert student_client.get(url, {"after": ["0", "1"]}).status_code == 400
    unknown = student_client.get(url, {"cursor": 0})
    assert unknown.status_code == 400
    assert unknown.json()["errors"] == {"cursor": ["This query parameter is not supported."]}
    assert outsider_client.get(url).status_code == 404
    assert teacher_client.head(url).status_code == 200
    assert (
        student_client.get(
            f"{THREADS}{thread['id']}/messages/",
            {"after_id": "9223372036854775808"},
        ).status_code
        == 400
    )


@pytest.mark.django_db
def test_read_cursor_requires_a_message_in_the_same_thread_and_body_is_bounded(tenant_a, as_role):
    teacher_client, _teacher, student_client, student, thread_a = _open_thread(as_role)
    other = teacher_client.post(
        THREADS,
        {"participant_ids": [student.pk], "first_body": "other thread"},
        format="json",
    ).json()["data"]
    other_message = teacher_client.get(f"{THREADS}{other['id']}/messages/").json()["data"][0]

    cross_thread = student_client.post(
        f"{THREADS}{thread_a['id']}/read/",
        {"through_message_id": other_message["id"]},
        format="json",
    )
    assert cross_thread.status_code == 404
    assert (
        student_client.post(
            f"{THREADS}{thread_a['id']}/read/",
            {"through_message_id": True},
            format="json",
        ).status_code
        == 400
    )
    assert (
        student_client.post(
            f"{THREADS}{thread_a['id']}/read/",
            {"through_message_id": 9_223_372_036_854_775_808},
            format="json",
        ).status_code
        == 400
    )
    oversized = teacher_client.post(
        f"{THREADS}{thread_a['id']}/messages/",
        {"body": "x" * 10_001},
        format="json",
    )
    assert oversized.status_code == 400
    assert oversized.json()["errors"] == {"body": ["Use at most 10,000 characters."]}


@pytest.mark.django_db
def test_database_guards_and_cutover_report_detect_non_service_writes(tenant_a, as_role):
    """ORM shortcuts cannot forge attribution, events, or a racing read cursor."""

    from apps.messaging.models import Message, ThreadParticipant, ThreadRealtimeEvent

    teacher_client, teacher, student_client, student, thread = _open_thread(as_role)
    thread_id = thread["id"]
    # Event sequence and message ids both start at one in a clean test schema,
    # but use the endpoint as the authority instead of relying on that accident.
    first_message_id = student_client.get(
        f"{THREADS}{thread_id}/events/",
        {"after": 0},
    ).json()["data"]["events"][0]["message_id"]
    other = teacher_client.post(
        THREADS,
        {"participant_ids": [student.pk], "first_body": "different thread"},
        format="json",
    ).json()["data"]
    other_message_id = student_client.get(
        f"{THREADS}{other['id']}/events/",
        {"after": 0},
    ).json()["data"]["events"][0]["message_id"]

    with schema_context(tenant_a.schema_name):
        event = ThreadRealtimeEvent.objects.get(thread_id=thread_id, sequence=1)
        teacher_seat = ThreadParticipant.objects.get(thread_id=thread_id, user_id=teacher.pk)
        student_seat = ThreadParticipant.objects.get(thread_id=thread_id, user_id=student.pk)

        with pytest.raises(DatabaseError), transaction.atomic():
            ThreadRealtimeEvent.objects.filter(pk=event.pk).update(message_id=other_message_id)
        with pytest.raises(DatabaseError), transaction.atomic():
            ThreadRealtimeEvent.objects.filter(pk=event.pk).delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            Message.objects.filter(pk=first_message_id).update(sender_attribution_status="resolved")
        with pytest.raises(DatabaseError), transaction.atomic():
            ThreadParticipant.objects.filter(pk=student_seat.pk).update(
                last_read_at=timezone.now(),
                last_read_message_id=other_message_id,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            ThreadParticipant.objects.filter(pk=student_seat.pk).update(thread_id=other["id"])
        with pytest.raises(DatabaseError), transaction.atomic():
            ThreadRealtimeEvent.objects.create(
                thread_id=thread_id,
                sequence=2,
                kind="message.created",
                actor_id=teacher.pk,
                actor_principal_kind=teacher_seat.principal_kind,
                actor_principal_id=teacher_seat.principal_id,
                message_id=first_message_id,
            )

        # A direct ORM message still passes exact-seat attribution, but cannot
        # silently enter the realtime protocol without the service's atomic
        # event allocation. The release preflight must stop that cutover.
        bypass = Message.objects.create(thread_id=thread_id, sender=teacher, body="cutover-canary")
        assert bypass.sender_attribution_status == "captured"

    output = StringIO()
    call_command(
        "review_messaging_realtime_cutover",
        "--schema",
        tenant_a.schema_name,
        stdout=output,
    )
    report = json.loads(output.getvalue())
    tenant_report = report["reports"][0]
    assert tenant_report["captured_messages_without_event"] == 1
    assert tenant_report["invalid_read_cursors"] == 0
    assert tenant_report["event_gap_threads"] == 0
    assert tenant_report["sequence_mismatch_threads"] == 0
    assert "cutover-canary" not in output.getvalue()
    with pytest.raises(CommandError, match="integrity blockers"):
        call_command(
            "review_messaging_realtime_cutover",
            "--schema",
            tenant_a.schema_name,
            "--fail-on-blocked",
            stdout=StringIO(),
        )


@pytest.mark.django_db
def test_event_recovery_is_three_bounded_queries_without_loading_message_content(
    tenant_a,
    as_role,
    django_assert_num_queries,
):
    from apps.messaging.models import Thread
    from apps.messaging.repositories.thread_repository import ThreadRepository

    teacher_client, _teacher, _student_client, _student, thread_payload = _open_thread(as_role)
    thread_id = thread_payload["id"]
    for index in range(4):
        response = teacher_client.post(
            f"{THREADS}{thread_id}/messages/",
            {"body": f"bounded event {index}"},
            format="json",
        )
        assert response.status_code == 201, response.content

    with schema_context(tenant_a.schema_name):
        thread = Thread.objects.only("id").get(pk=thread_id)
        with django_assert_num_queries(3) as captured:
            page = ThreadRepository().event_page(thread=thread, after=0, limit=2)
            assert [event.sequence for event in page.events] == [1, 2]
            assert page.has_more is True
            assert page.high_watermark == 5

        captured_sql = " ".join(query["sql"] for query in captured.captured_queries)
        assert 'JOIN "messaging_message"' not in captured_sql
        assert '"messaging_message"."body"' not in captured_sql


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_websocket_streams_scoped_pointers_and_recovers_missed_events(tenant_a, as_role):
    teacher_client, _teacher, student_client, _student, thread = await sync_to_async(_open_thread)(as_role)
    thread_id = thread["id"]
    communicator = WebsocketCommunicator(
        application,
        f"/ws/messaging/threads/{thread_id}/",
        headers=HOST,
        subprotocols=[f"bearer.{_access_token(student_client)}"],
    )
    connected, protocol = await communicator.connect()
    assert connected is True
    assert protocol is None
    ready = await communicator.receive_json_from(timeout=2)
    assert ready["type"] == "thread.ready"
    assert ready["payload"]["thread_id"] == thread_id
    assert ready["payload"]["event_delivery"] == "best_effort_pointer_with_durable_recovery"
    assert ready["payload"]["live_ordering"] == "not_guaranteed"
    assert ready["payload"]["recovery_ordering"] == "sequence_ascending"
    assert ready["payload"]["deduplication_key"] == "sequence"
    assert ready["payload"]["gap_recovery"] == "thread.sync"
    assert ready["payload"]["capabilities"] == {
        "missed_event_recovery": True,
        "read_receipts": True,
        "typing": False,
        "delivery_receipts": False,
        "presence": "not_provided",
    }

    await communicator.send_json_to({"type": "thread.sync", "after": 0, "limit": 10})
    synced = await communicator.receive_json_from(timeout=2)
    assert synced["type"] == "thread.sync"
    first_cursor = synced["payload"]["next_cursor"]

    def _post_message():
        return teacher_client.post(
            f"{THREADS}{thread_id}/messages/",
            {"body": "never place this body in Redis"},
            format="json",
        )

    posted = await sync_to_async(_post_message)()
    assert posted.status_code == 201, posted.content
    event = await communicator.receive_json_from(timeout=2)
    assert event["type"] == "thread.event"
    assert event["payload"]["sequence"] == first_cursor + 1
    assert event["payload"]["message_id"] == posted.json()["data"]["id"]
    assert "never place this body in Redis" not in repr(event)

    # The channel layer is only a hint transport. Even an internal producer
    # that repeats the cursor with poisoned fields cannot forge the pointer;
    # the consumer re-resolves immutable database evidence. The duplicate is
    # intentional and demonstrates why clients deduplicate by sequence.
    layer = get_channel_layer()
    assert layer is not None
    from infrastructure.websocket.groups import messaging_thread_group

    await layer.group_send(
        messaging_thread_group(tenant_a.schema_name, thread_id),
        {
            "type": "messaging.thread.event",
            "thread_id": thread_id,
            "sequence": first_cursor + 1,
            "event_kind": "read.updated",
            "message_id": 9_223_372_036_854_775_807,
            "actor_principal_kind": "staff",
            "actor_principal_id": 9_223_372_036_854_775_807,
            "body": "poisoned producer payload",
        },
    )
    canonical_duplicate = await communicator.receive_json_from(timeout=2)
    assert canonical_duplicate == event
    assert "poisoned producer payload" not in repr(canonical_duplicate)

    await communicator.send_json_to({"type": "thread.sync", "after": first_cursor})
    recovered = await communicator.receive_json_from(timeout=2)
    assert recovered["payload"]["events"] == [
        {
            "thread_id": thread_id,
            "sequence": first_cursor + 1,
            "kind": "message.created",
            "message_id": posted.json()["data"]["id"],
            "actor_principal_kind": "teacher",
            "actor_principal_id": recovered["payload"]["events"][0]["actor_principal_id"],
            "created_at": recovered["payload"]["events"][0]["created_at"],
        }
    ]
    await communicator.disconnect()
    await communicator.wait(timeout=2)


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_websocket_denies_outsider_and_rechecks_membership_before_push(tenant_a, as_role):
    _teacher_client, _teacher, student_client, _student, thread = await sync_to_async(_open_thread)(as_role)
    outsider_client, _outsider = await sync_to_async(as_role)(Role.TEACHER)
    denied = WebsocketCommunicator(
        application,
        f"/ws/messaging/threads/{thread['id']}/",
        headers=HOST,
        subprotocols=[f"bearer.{_access_token(outsider_client)}"],
    )
    connected, code = await denied.connect()
    assert connected is False
    assert code == 4403
    await denied.wait(timeout=2)

    communicator = WebsocketCommunicator(
        application,
        f"/ws/messaging/threads/{thread['id']}/",
        headers=HOST,
        subprotocols=[f"bearer.{_access_token(student_client)}"],
    )
    connected, _protocol = await communicator.connect()
    assert connected is True
    await communicator.receive_json_from(timeout=2)  # ready

    def _remove_exact_seat():
        from apps.messaging.models import ThreadParticipant

        with schema_context(tenant_a.schema_name):
            ThreadParticipant.objects.filter(
                thread_id=thread["id"],
                user_id=_student.pk,
            ).delete()

    await sync_to_async(_remove_exact_seat)()
    layer = get_channel_layer()
    assert layer is not None
    from infrastructure.websocket.groups import messaging_thread_group

    await layer.group_send(
        messaging_thread_group(tenant_a.schema_name, thread["id"]),
        {
            "type": "messaging.thread.event",
            "thread_id": thread["id"],
            "sequence": thread["realtime_cursor"],
            "body": "must never relay",
        },
    )
    output = await communicator.receive_output(timeout=2)
    assert output == {"type": "websocket.close", "code": 4403}
    await communicator.wait(timeout=2)


@pytest.mark.channels
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_websocket_bounds_cursor_and_rate_limits_recovery(tenant_a, as_role):
    _teacher_client, _teacher, student_client, _student, thread = await sync_to_async(_open_thread)(as_role)
    communicator = WebsocketCommunicator(
        application,
        f"/ws/messaging/threads/{thread['id']}/",
        headers=HOST,
        subprotocols=[f"bearer.{_access_token(student_client)}"],
    )
    connected, _protocol = await communicator.connect()
    assert connected is True
    await communicator.receive_json_from(timeout=2)  # ready

    await communicator.send_json_to({"type": "thread.sync", "after": 9_223_372_036_854_775_808})
    invalid = await communicator.receive_json_from(timeout=2)
    assert invalid == {
        "type": "protocol.error",
        "payload": {"code": "invalid_sync_request"},
    }

    for _index in range(6):
        await communicator.send_json_to(
            {"type": "thread.sync", "after": thread["realtime_cursor"], "limit": 1}
        )
        synced = await communicator.receive_json_from(timeout=2)
        assert synced["type"] == "thread.sync"
    await communicator.send_json_to({"type": "thread.sync", "after": thread["realtime_cursor"], "limit": 1})
    assert await communicator.receive_output(timeout=2) == {
        "type": "websocket.close",
        "code": 4429,
    }
    await communicator.wait(timeout=2)


def test_messaging_realtime_openapi_is_explicit_and_pointer_only():
    from core.openapi import build_schema

    schema = build_schema("config.urls")
    events = schema["paths"]["/api/v1/messaging/threads/{pk}/events/"]
    assert {method for method in events if method in {"get", "head", "post"}} == {"get", "head"}
    assert {parameter["name"] for parameter in events["get"]["parameters"]} == {
        "after",
        "limit",
    }
    event_schema = events["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    event_item = event_schema["properties"]["data"]["properties"]["events"]["items"]
    assert "body" not in event_item["properties"]
    assert "attachments" not in event_item["properties"]
    read = schema["paths"]["/api/v1/messaging/threads/{pk}/read/"]
    assert {method for method in read if method in {"get", "head", "post"}} == {"post"}
    assert read["post"]["security"] == [
        {"sessionAuth": []},
        {"cookieSession": [], "csrfHeader": []},
    ]
