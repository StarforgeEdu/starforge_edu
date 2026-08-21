"""F8-1 — media-based placement question types: reading (passage), listening (audio
prompt), speaking (audio answer). All human-marked (no answer key), carry `media` shown
to the taker, and score via the same manual-mark path as writing."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TESTS = "/api/v1/placement/tests/"
ATTEMPTS = "/api/v1/placement/attempts/"


def test_add_media_questions_are_human_graded(tenant_a):
    from apps.placement import services
    from apps.placement.models import PlacementQuestion

    with schema_context(tenant_a.schema_name):
        from apps.users.tests.factories import UserFactory

        test = services.create_test(title="Skills", created_by=UserFactory.create())
        reading = services.add_question(
            test=test,
            prompt="Read the passage and answer.",
            question_type="reading",
            media={"passage": "Once upon a time..."},
            points=5,
        )
        listening = services.add_question(
            test=test,
            prompt="What did the speaker order?",
            question_type="listening",
            media={"audio_url": "https://cdn.example/clip.mp3"},
            points=5,
        )
        speaking = services.add_question(
            test=test,
            prompt="Describe your hometown.",
            question_type="speaking",
            points=10,
        )
        for q in (reading, listening, speaking):
            assert q.question_type in PlacementQuestion.HUMAN_GRADED_TYPES
            assert q.correct_answer is None
        assert reading.media == {"passage": "Once upon a time..."}
        assert listening.media == {"audio_url": "https://cdn.example/clip.mp3"}
        assert speaking.media == {}  # optional


def test_media_question_rejects_an_answer_key(tenant_a):
    from apps.placement import services
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        from apps.users.tests.factories import UserFactory

        test = services.create_test(title="T", created_by=UserFactory.create())
        with pytest.raises(ValidationException) as exc:
            services.add_question(
                test=test, prompt="Listen", question_type="listening", correct_answer="anything"
            )
        assert exc.value.code == "writing_has_no_answer"


def test_media_must_be_an_object(tenant_a):
    from apps.placement import services
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        from apps.users.tests.factories import UserFactory

        test = services.create_test(title="T", created_by=UserFactory.create())
        with pytest.raises(ValidationException) as exc:
            services.add_question(
                test=test, prompt="Read", question_type="reading", media=["not", "a", "dict"]
            )
        assert exc.value.code == "invalid_media"


def test_taker_sees_media_but_not_the_answer_key(tenant_a):
    from apps.placement import services
    from apps.placement.presenters import attempt_question_to_dict, placement_question_to_dict

    with schema_context(tenant_a.schema_name):
        from apps.users.tests.factories import UserFactory

        test = services.create_test(title="T", created_by=UserFactory.create())
        q = services.add_question(
            test=test,
            prompt="Listen",
            question_type="listening",
            media={"audio_url": "https://cdn.example/a.mp3"},
        )
        taker = attempt_question_to_dict(q)
        staff = placement_question_to_dict(q)
        assert taker["media"] == {"audio_url": "https://cdn.example/a.mp3"}  # taker needs it
        assert "correct_answer" not in taker  # but never the key
        assert staff["media"] == taker["media"]
        assert "correct_answer" in staff


def _speaking_attempt(tenant, user_in, as_user):
    """An approved test with a speaking question, assigned to a lead who submits an audio-key
    answer -> a GRADED attempt awaiting the human speaking mark."""
    from apps.org.tests.factories import BranchFactory
    from apps.placement import services
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    teacher_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    hod_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    lead_u = user_in(tenant, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant.schema_name):
        lead = StudentProfileFactory.create(user=lead_u, branch=branch, status=StudentProfile.Status.LEAD)
        test = services.create_test(title="Speaking", created_by=teacher_u, branch=branch)
        q_speak = services.add_question(
            test=test, prompt="Describe your city.", question_type="speaking", points=10
        )
        services.submit_for_review(test=test)
        test = services.approve_test(test=test, approver=hod_u)
        attempt = services.assign_test(test=test, student=lead, assigned_by=hod_u)
        # The speaking answer is the S3 key of the taker's uploaded audio (a string value).
        attempt = services.submit_attempt(
            attempt=attempt, answers=[{"question": q_speak.id, "response": "uploads/answer-audio-1.webm"}]
        )
    return {"staff": as_user(tenant, hod_u), "attempt": attempt, "q_speak": q_speak}


def test_manual_marking_scores_a_speaking_answer(tenant_a, user_in, as_user):
    s = _speaking_attempt(tenant_a, user_in, as_user)
    r = s["staff"].post(
        f"{ATTEMPTS}{s['attempt'].id}/mark-writing-manual/",
        {"marks": [{"question": s["q_speak"].id, "score": 8}]},
        format="json",
    )
    assert r.status_code == 200, r.content
    body = r.json()["data"]
    assert body["max_score"] == 10  # the speaking question now counts
    assert body["score"] == 8
    assert body["level"] == "advanced"  # 80%
