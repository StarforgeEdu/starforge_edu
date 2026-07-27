"""F1-3 — AI placement-test generation: the manager asks the AI to draft questions
onto a DRAFT test; the JSON output is parsed, validated, and the valid questions are
appended (reusing the apps.ai budget/redaction pipeline). Tolerant of bad output.
"""

from __future__ import annotations

import json

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TESTS = "/api/v1/placement/tests/"


def _seed_placement_ai(tenant, *, enabled=True):
    from apps.ai.tests.factories import AIPromptFactory, make_budget
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        AIPromptFactory(
            feature="placement_generation",
            version=1,
            system_prompt="Output a JSON array of placement questions.",
            user_template="Subject: {subject}\nCount: {count}\nDifficulty: {difficulty}\nTopic: {topic}",
            max_output_tokens=4096,
            effort="high",
            token_cost_cap=12000,
            is_active=True,
        )
        make_budget(daily_token_limit=1_000_000, monthly_token_limit=10_000_000, is_enabled=True)
        cs = CenterSettings.load()
        cs.ai_exam_generation_enabled = enabled
        cs.save()


def _draft_test(tenant, branch, builder):
    from apps.placement.services import create_test

    with schema_context(tenant.schema_name):
        return create_test(title="EN placement", created_by=builder, branch=branch)


def _setup(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    teacher_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    return {
        "branch": branch,
        "teacher_u": teacher_u,
        "teacher": as_user(tenant, teacher_u),
    }


_GOOD_QUESTIONS = [
    {"prompt": "2+2?", "question_type": "single_choice", "options": ["3", "4"], "correct_answer": "4", "points": 2},
    {"prompt": "Sky is blue?", "question_type": "true_false", "correct_answer": True},
    {"prompt": "Describe your day.", "question_type": "writing"},
    # invalid: a single-choice with only one option -> skipped, not fatal
    {"prompt": "bad", "question_type": "single_choice", "options": ["only"], "correct_answer": "only"},
]


def _mock_complete(monkeypatch, text):
    from celery_tasks import ai_tasks

    monkeypatch.setattr(
        ai_tasks,
        "complete",
        lambda **kw: {"text": text, "usage": {"input_tokens": 10, "output_tokens": 20}},
    )


def test_generate_applies_only_valid_questions(tenant_a, user_in, as_user, monkeypatch):
    from celery_tasks import ai_tasks

    s = _setup(tenant_a, user_in, as_user)
    _seed_placement_ai(tenant_a)
    _mock_complete(monkeypatch, json.dumps(_GOOD_QUESTIONS))
    test = _draft_test(tenant_a, s["branch"], s["teacher_u"])
    with schema_context(tenant_a.schema_name):
        from apps.ai.models import AIRequest
        from apps.placement.models import PlacementQuestion
        from apps.placement.services import request_placement_generation

        ai_request = request_placement_generation(test=test, count=4, requested_by=s["teacher_u"])
        ai_tasks.run_placement_generation(
            ai_request.pk, params={"test_id": test.id, "count": 4, "difficulty": "medium", "topic": ""}
        )
        ai_request.refresh_from_db()
        assert ai_request.status == AIRequest.Status.SUCCEEDED
        # 3 of the 4 generated questions are valid (the 1-option single_choice is skipped)
        assert PlacementQuestion.objects.filter(test=test).count() == 3


def test_generate_tolerates_unparseable_output(tenant_a, user_in, as_user, monkeypatch):
    from celery_tasks import ai_tasks

    s = _setup(tenant_a, user_in, as_user)
    _seed_placement_ai(tenant_a)
    _mock_complete(monkeypatch, "Sorry, I can't do that.")  # not JSON
    test = _draft_test(tenant_a, s["branch"], s["teacher_u"])
    with schema_context(tenant_a.schema_name):
        from apps.ai.models import AIRequest
        from apps.placement.models import PlacementQuestion
        from apps.placement.services import request_placement_generation

        ai_request = request_placement_generation(test=test, count=4, requested_by=s["teacher_u"])
        ai_tasks.run_placement_generation(ai_request.pk, params={"test_id": test.id, "count": 4})
        ai_request.refresh_from_db()
        # generation "succeeded" (it returned text); parsing added nothing — never a hard failure
        assert ai_request.status == AIRequest.Status.SUCCEEDED
        assert PlacementQuestion.objects.filter(test=test).count() == 0


def test_generate_strips_markdown_fences(tenant_a, user_in, as_user, monkeypatch):
    from celery_tasks import ai_tasks

    s = _setup(tenant_a, user_in, as_user)
    _seed_placement_ai(tenant_a)
    fenced = "```json\n" + json.dumps(_GOOD_QUESTIONS[:1]) + "\n```"
    _mock_complete(monkeypatch, fenced)
    test = _draft_test(tenant_a, s["branch"], s["teacher_u"])
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementQuestion
        from apps.placement.services import request_placement_generation

        ai_request = request_placement_generation(test=test, count=1, requested_by=s["teacher_u"])
        ai_tasks.run_placement_generation(ai_request.pk, params={"test_id": test.id, "count": 1})
        assert PlacementQuestion.objects.filter(test=test).count() == 1


def test_generate_does_not_mutate_a_non_draft_test(tenant_a, user_in, as_user, monkeypatch):
    """If the test leaves DRAFT between request and task completion, apply is a no-op."""
    from celery_tasks import ai_tasks

    s = _setup(tenant_a, user_in, as_user)
    _seed_placement_ai(tenant_a)
    _mock_complete(monkeypatch, json.dumps(_GOOD_QUESTIONS[:1]))
    test = _draft_test(tenant_a, s["branch"], s["teacher_u"])
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementQuestion, PlacementTest
        from apps.placement.services import request_placement_generation

        ai_request = request_placement_generation(test=test, count=1, requested_by=s["teacher_u"])
        # the test advances out of DRAFT before the task runs
        PlacementTest.objects.filter(pk=test.id).update(status=PlacementTest.Status.PENDING)
        ai_tasks.run_placement_generation(ai_request.pk, params={"test_id": test.id, "count": 1})
        assert PlacementQuestion.objects.filter(test=test).count() == 0


def test_generate_endpoint_returns_202(tenant_a, user_in, as_user, monkeypatch):
    s = _setup(tenant_a, user_in, as_user)
    _seed_placement_ai(tenant_a)
    _mock_complete(monkeypatch, json.dumps(_GOOD_QUESTIONS))
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    r = s["teacher"].post(f"{TESTS}{tid}/generate/", {"count": 5, "difficulty": "easy"}, format="json")
    assert r.status_code == 202, r.content
    assert r.json()["data"]["request_id"]


def test_generate_endpoint_blocked_when_feature_disabled(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _seed_placement_ai(tenant_a, enabled=False)  # centre has AI generation off
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    r = s["teacher"].post(f"{TESTS}{tid}/generate/", {"count": 5}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "feature_disabled"


def test_generate_endpoint_rejects_a_non_draft_test(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _seed_placement_ai(tenant_a)
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    s["teacher"].post(f"{TESTS}{tid}/questions/", {
        "prompt": "q", "question_type": "true_false", "correct_answer": True,
    }, format="json")
    s["teacher"].post(f"{TESTS}{tid}/submit/", {}, format="json")  # -> PENDING
    r = s["teacher"].post(f"{TESTS}{tid}/generate/", {"count": 5}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "test_not_draft"


def test_apply_skips_unknown_types_and_bounds_points_without_failing(tenant_a, user_in, as_user):
    """Never-raise invariant: a hallucinated question type (incl. one longer than the
    varchar(16) column) or an out-of-range points value is skipped/clamped, never a
    DB error that discards the whole batch."""
    s = _setup(tenant_a, user_in, as_user)
    test = _draft_test(tenant_a, s["branch"], s["teacher_u"])
    payload = json.dumps([
        {"prompt": "ok", "question_type": "true_false", "correct_answer": True, "points": 40000},
        {"prompt": "longtype", "question_type": "fill_in_the_blank_extra_long", "correct_answer": "x"},
        {"prompt": "shorttype", "question_type": "matching", "correct_answer": "x"},
    ])
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementQuestion
        from apps.placement.services import apply_generated_questions

        added = apply_generated_questions(test_id=test.id, output_text=payload)
        assert added == 1  # only the true_false survives; the two unknown types are skipped
        q = PlacementQuestion.objects.get(test=test)
        assert q.question_type == "true_false"
        assert q.points == 100  # clamped from 40000, no smallint overflow


def test_apply_is_idempotent_on_reapply(tenant_a, user_in, as_user):
    """A task retry re-runs the persist hook; dedup-by-prompt stops a double-apply."""
    s = _setup(tenant_a, user_in, as_user)
    test = _draft_test(tenant_a, s["branch"], s["teacher_u"])
    payload = json.dumps(_GOOD_QUESTIONS)
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementQuestion
        from apps.placement.services import apply_generated_questions

        assert apply_generated_questions(test_id=test.id, output_text=payload) == 3
        assert apply_generated_questions(test_id=test.id, output_text=payload) == 0  # no duplicates
        assert PlacementQuestion.objects.filter(test=test).count() == 3


def test_apply_tolerates_a_vanished_test(tenant_a):
    with schema_context(tenant_a.schema_name):
        from apps.placement.services import apply_generated_questions

        assert apply_generated_questions(test_id=999999, output_text="[]") == 0  # no DoesNotExist


def test_student_cannot_generate(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _seed_placement_ai(tenant_a)
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    student = as_user(tenant_a, user_in(tenant_a, roles=[Role.STUDENT], branch=s["branch"]))
    assert student.post(f"{TESTS}{tid}/generate/", {"count": 5}, format="json").status_code == 403
