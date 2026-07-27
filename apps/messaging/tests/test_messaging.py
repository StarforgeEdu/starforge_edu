"""F4-4 — in-app messaging: threads, messages, strict participant isolation,
student↔staff safeguarding, unread counts, and realtime notify."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

THREADS = "/api/v1/messaging/threads/"


def _rows(body):
    # Layered endpoints return {success, data, pagination}; a still-DRF endpoint returns
    # {count, next, previous, results}; some actions returned a bare list.
    if isinstance(body, dict):
        if "data" in body:
            return body["data"]
        if "results" in body:
            return body["results"]
    return body


def test_create_thread_and_exchange_messages(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)

    created = teacher_client.post(
        THREADS,
        {"subject": "Homework", "participant_ids": [student.id], "first_body": "Please submit."},
        format="json",
    )
    assert created.status_code == 201, created.content
    tid = created.json()["data"]["id"]

    # the student sees the thread
    assert any(t["id"] == tid for t in _rows(student_client.get(THREADS).json()))

    # the student replies
    reply = student_client.post(f"{THREADS}{tid}/messages/", {"body": "Done!"}, format="json")
    assert reply.status_code == 201, reply.content

    # both messages are visible to a participant
    msgs = _rows(teacher_client.get(f"{THREADS}{tid}/messages/").json())
    assert [m["body"] for m in msgs] == ["Please submit.", "Done!"]


def test_non_participant_cannot_access_thread(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    _sc, student = as_role(Role.STUDENT)
    outsider_client, _o = as_role(Role.TEACHER)

    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "hi"}, format="json"
    ).json()["data"]["id"]

    # an outsider can neither read nor post in a thread they're not part of
    assert outsider_client.get(f"{THREADS}{tid}/").status_code == 404
    assert outsider_client.get(f"{THREADS}{tid}/messages/").status_code == 404
    assert outsider_client.post(f"{THREADS}{tid}/messages/", {"body": "x"}, format="json").status_code == 404
    assert _rows(outsider_client.get(THREADS).json()) == []


def test_student_cannot_message_another_student(tenant_a, as_role):
    student_client, _s = as_role(Role.STUDENT)
    _oc, other_student = as_role(Role.STUDENT)
    r = student_client.post(
        THREADS, {"participant_ids": [other_student.id], "first_body": "hey"}, format="json"
    )
    assert r.status_code == 403
    assert r.json()["code"] == "non_staff_recipient"


def test_student_can_message_a_teacher(tenant_a, as_role):
    student_client, _s = as_role(Role.STUDENT)
    _tc, teacher = as_role(Role.TEACHER)
    r = student_client.post(
        THREADS, {"participant_ids": [teacher.id], "first_body": "a question"}, format="json"
    )
    assert r.status_code == 201, r.content


def test_unread_count_tracks_reads(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "m1"}, format="json"
    ).json()["data"]["id"]

    def student_unread():
        return next(t["unread_count"] for t in _rows(student_client.get(THREADS).json()) if t["id"] == tid)

    assert student_unread() == 1  # the teacher's opener
    student_client.post(f"{THREADS}{tid}/read/", {}, format="json")
    assert student_unread() == 0  # caught up
    teacher_client.post(f"{THREADS}{tid}/messages/", {"body": "m2"}, format="json")
    assert student_unread() == 1  # a new one arrived


def test_message_notifies_the_recipient(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    teacher_client.post(THREADS, {"participant_ids": [student.id], "first_body": "ping"}, format="json")
    events = {n["event_type"] for n in student_client.get("/api/v1/notifications/").json()["results"]}
    assert "message.received" in events


def test_empty_message_rejected(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    _sc, student = as_role(Role.STUDENT)
    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "hi"}, format="json"
    ).json()["data"]["id"]
    assert teacher_client.post(f"{THREADS}{tid}/messages/", {"body": "   "}, format="json").status_code == 400


def test_thread_needs_another_participant(tenant_a, as_role):
    teacher_client, teacher = as_role(Role.TEACHER)
    # a thread with only yourself is rejected
    r = teacher_client.post(THREADS, {"participant_ids": [teacher.id], "first_body": "note"}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "thread_needs_participant"


def test_role_without_messaging_is_denied(tenant_a, as_role):
    sec_client, _s = as_role(Role.SECURITY)  # security holds no messaging permission
    assert sec_client.get(THREADS).status_code == 403
    assert (
        sec_client.post(THREADS, {"participant_ids": [1], "first_body": "x"}, format="json").status_code
        == 403
    )


# --------------------------------------------------------------------------- #
# review hardening
# --------------------------------------------------------------------------- #
def test_staff_cannot_open_a_two_student_thread(tenant_a, as_role):
    # The safeguarding invariant is on the participant SET, not just the opener:
    # even a teacher cannot co-locate two students (no unsupervised peer channel).
    teacher_client, _t = as_role(Role.TEACHER)
    _s1c, s1 = as_role(Role.STUDENT)
    _s2c, s2 = as_role(Role.STUDENT)
    r = teacher_client.post(
        THREADS, {"participant_ids": [s1.id, s2.id], "first_body": "group"}, format="json"
    )
    assert r.status_code == 403
    assert r.json()["code"] == "non_staff_recipient"


def test_teacher_parent_student_thread_allowed(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    _pc, parent = as_role(Role.PARENT)
    _sc, student = as_role(Role.STUDENT)
    # one student + a parent + the teacher is fine (a conference thread)
    r = teacher_client.post(
        THREADS, {"participant_ids": [parent.id, student.id], "first_body": "meeting"}, format="json"
    )
    assert r.status_code == 201, r.content


def test_revoking_messaging_write_makes_a_role_read_only(tenant_a, as_role):
    from apps.access.services import set_override

    teacher_client, _t = as_role(Role.TEACHER)
    _sc, student = as_role(Role.STUDENT)
    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "hi"}, format="json"
    ).json()["data"]["id"]

    with schema_context(tenant_a.schema_name):
        set_override(role=Role.TEACHER, permission="messaging:write", effect="revoke")

    # reading the thread still works...
    assert teacher_client.get(f"{THREADS}{tid}/messages/").status_code == 200
    # ...but posting is now denied (write was revoked)
    assert teacher_client.post(f"{THREADS}{tid}/messages/", {"body": "x"}, format="json").status_code == 403


def test_membershipless_participant_rejected(tenant_a, as_role):
    from apps.users.tests.factories import UserFactory

    teacher_client, _t = as_role(Role.TEACHER)
    with schema_context(tenant_a.schema_name):
        orphan = UserFactory.create()  # active user, but no RoleMembership in this center
    r = teacher_client.post(THREADS, {"participant_ids": [orphan.id], "first_body": "hi"}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "unknown_participant"


def test_attachment_only_message_allowed(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    _sc, student = as_role(Role.STUDENT)
    created = teacher_client.post(
        THREADS,
        {"participant_ids": [student.id], "attachments": ["s3://uploads/photo.jpg"]},
        format="json",
    )
    assert created.status_code == 201, created.content
    msgs = _rows(teacher_client.get(f"{THREADS}{created.json()['data']['id']}/messages/").json())
    assert len(msgs) == 1
    assert msgs[0]["attachments"] == ["s3://uploads/photo.jpg"]
    assert msgs[0]["body"] == ""


def test_unread_excludes_your_own_messages(tenant_a, as_role):
    teacher_client, _t = as_role(Role.TEACHER)
    student_client, student = as_role(Role.STUDENT)
    tid = teacher_client.post(
        THREADS, {"participant_ids": [student.id], "first_body": "hi"}, format="json"
    ).json()["data"]["id"]
    student_client.post(f"{THREADS}{tid}/read/", {}, format="json")
    student_client.post(f"{THREADS}{tid}/messages/", {"body": "my reply"}, format="json")
    rows = _rows(student_client.get(THREADS).json())
    assert next(t["unread_count"] for t in rows if t["id"] == tid) == 0  # own message isn't unread
