"""F8-3 — AI marking of placement writing answers: a manager requests AI marking of a
submitted attempt; each writing answer is scored (clamped to its points), is_correct
is set so it now counts, and the attempt grade + academic_level are recomputed.
"""

from __future__ import annotations

import json

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

ATTEMPTS = "/api/v1/placement/attempts/"


def _seed_marking_ai(tenant, *, enabled=True):
    from apps.ai.tests.factories import AIPromptFactory, make_budget

    with schema_context(tenant.schema_name):
        AIPromptFactory(
            feature="writing_marking",
            version=1,
            system_prompt="Mark the writing answers.",
            user_template="Mark these:\n{items}",
            max_output_tokens=1024,
            effort="medium",
            token_cost_cap=4000,
            is_active=True,
        )
        make_budget(daily_token_limit=1_000_000, monthly_token_limit=10_000_000, is_enabled=enabled)


def _setup(tenant, user_in, as_user):
    """An APPROVED test (single_choice 2pts + writing 8pts), assigned to a lead who
    submits a correct objective answer + a writing answer. Returns the graded attempt."""
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
        lead = StudentProfileFactory.create(
            user=lead_u, branch=branch, status=StudentProfile.Status.LEAD
        )
        test = services.create_test(title="EN", created_by=teacher_u, branch=branch)
        q_obj = services.add_question(
            test=test, prompt="2+2?", question_type="single_choice", options=["3", "4"],
            correct_answer="4", points=2,
        )
        q_write = services.add_question(
            test=test, prompt="Write about your day.", question_type="writing", points=8
        )
        services.submit_for_review(test=test)
        test = services.approve_test(test=test, approver=hod_u)
        attempt = services.assign_test(test=test, student=lead, assigned_by=hod_u)
        attempt = services.submit_attempt(
            attempt=attempt,
            answers=[
                {"question": q_obj.id, "response": "4"},
                {"question": q_write.id, "response": "It was a productive and lovely day."},
            ],
        )
    return {
        "branch": branch,
        "staff": as_user(tenant, hod_u),
        "lead_c": as_user(tenant, lead_u),
        "attempt": attempt,
        "q_obj": q_obj,
        "q_write": q_write,
    }


def _mock_marks(monkeypatch, marks_json):
    from celery_tasks import ai_tasks

    monkeypatch.setattr(
        ai_tasks,
        "complete",
        lambda **kw: {"text": marks_json, "usage": {"input_tokens": 10, "output_tokens": 10}},
    )


def test_submit_grades_objective_only_then_marking_folds_in_writing(tenant_a, user_in, as_user, monkeypatch):
    from celery_tasks import ai_tasks

    s = _setup(tenant_a, user_in, as_user)
    # at submit, writing is excluded: 2/2 objective -> advanced
    assert s["attempt"].score == 2
    assert s["attempt"].max_score == 2
    assert s["attempt"].level == "advanced"

    _mock_marks(monkeypatch, json.dumps([{"question_id": s["q_write"].id, "score": 4}]))
    with schema_context(tenant_a.schema_name):
        from apps.ai.models import AIRequest
        from apps.placement.models import PlacementAnswer, PlacementAttempt
        from apps.placement.services import request_writing_marking

        ai_request = request_writing_marking(attempt=s["attempt"], requested_by=None)
        ai_tasks.run_writing_marking(ai_request.pk, params={"attempt_id": s["attempt"].id})
        ai_request.refresh_from_db()
        assert ai_request.status == AIRequest.Status.SUCCEEDED

        wa = PlacementAnswer.objects.get(attempt=s["attempt"], question=s["q_write"])
        assert wa.awarded_points == 4
        assert wa.is_correct is True  # now marked -> counts toward the grade

        attempt = PlacementAttempt.objects.get(pk=s["attempt"].id)
        assert attempt.max_score == 10  # 2 objective + 8 writing
        assert attempt.score == 6  # 2 + 4
        assert attempt.level == "intermediate"  # 60% -> recomputed down from advanced


def test_marking_clamps_and_skips_bad_items_without_failing(tenant_a, user_in, as_user, monkeypatch):
    from celery_tasks import ai_tasks

    s = _setup(tenant_a, user_in, as_user)
    # an over-range score (99 > 8 max) is clamped; an unknown question_id is skipped
    _mock_marks(
        monkeypatch,
        json.dumps([{"question_id": s["q_write"].id, "score": 99}, {"question_id": 999999, "score": 5}]),
    )
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementAnswer
        from apps.placement.services import request_writing_marking

        ai_request = request_writing_marking(attempt=s["attempt"])
        ai_tasks.run_writing_marking(ai_request.pk, params={"attempt_id": s["attempt"].id})
        wa = PlacementAnswer.objects.get(attempt=s["attempt"], question=s["q_write"])
        assert wa.awarded_points == 8  # clamped to the question's points


def test_marking_is_idempotent_on_reapply(tenant_a, user_in, as_user, monkeypatch):
    s = _setup(tenant_a, user_in, as_user)
    _mock_marks(monkeypatch, json.dumps([{"question_id": s["q_write"].id, "score": 4}]))
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementAttempt
        from apps.placement.services import apply_writing_marks

        payload = json.dumps([{"question_id": s["q_write"].id, "score": 4}])
        apply_writing_marks(attempt_id=s["attempt"].id, output_text=payload)
        apply_writing_marks(attempt_id=s["attempt"].id, output_text=payload)  # re-run overwrites
        attempt = PlacementAttempt.objects.get(pk=s["attempt"].id)
        assert attempt.score == 6  # not double-counted (would be 10 if added twice)


def test_marking_tolerates_unparseable_output(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementAttempt
        from apps.placement.services import apply_writing_marks

        assert apply_writing_marks(attempt_id=s["attempt"].id, output_text="not json") == 0
        attempt = PlacementAttempt.objects.get(pk=s["attempt"].id)
        assert attempt.level == "advanced"  # unchanged from submit


def test_writing_scored_zero_still_counts_in_the_denominator(tenant_a, user_in, as_user):
    """A writing answer scored 0 is still MARKED (is_correct=False) and counts toward
    max_score — so the level reflects the full test, not just the objective part."""
    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementAnswer, PlacementAttempt
        from apps.placement.services import apply_writing_marks

        apply_writing_marks(
            attempt_id=s["attempt"].id,
            output_text=json.dumps([{"question_id": s["q_write"].id, "score": 0}]),
        )
        wa = PlacementAnswer.objects.get(attempt=s["attempt"], question=s["q_write"])
        assert wa.is_correct is False  # marked, not null
        attempt = PlacementAttempt.objects.get(pk=s["attempt"].id)
        assert attempt.max_score == 10  # 2 objective + 8 writing (counted at 0)
        assert attempt.score == 2
        assert attempt.level == "beginner"  # 2/10 = 20%


def test_marking_does_not_clobber_a_non_prospective_students_level(tenant_a, user_in, as_user):
    """If the lead has since been enrolled + their academic_level hand-curated, a later
    marking recomputes the ATTEMPT but must not overwrite the curated profile level."""
    s = _setup(tenant_a, user_in, as_user)
    sid = s["attempt"].student_id
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementAttempt
        from apps.placement.services import apply_writing_marks
        from apps.students.models import StudentProfile

        StudentProfile.objects.filter(pk=sid).update(
            status=StudentProfile.Status.ACTIVE, academic_level="B2"
        )
        apply_writing_marks(
            attempt_id=s["attempt"].id,
            output_text=json.dumps([{"question_id": s["q_write"].id, "score": 4}]),
        )
        assert PlacementAttempt.objects.get(pk=s["attempt"].id).level == "intermediate"  # attempt recomputed
        assert StudentProfile.objects.get(pk=sid).academic_level == "B2"  # curated level preserved


def test_mark_writing_endpoint_202(tenant_a, user_in, as_user, monkeypatch):
    s = _setup(tenant_a, user_in, as_user)
    _seed_marking_ai(tenant_a)
    r = s["staff"].post(f"{ATTEMPTS}{s['attempt'].id}/mark-writing/", {}, format="json")
    assert r.status_code == 202, r.content
    assert r.json()["data"]["request_id"]


def test_lead_cannot_mark_their_own_writing(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _seed_marking_ai(tenant_a)
    # the lead holds no placement:write -> cannot mark
    assert s["lead_c"].post(f"{ATTEMPTS}{s['attempt'].id}/mark-writing/", {}, format="json").status_code == 403


def test_cannot_mark_an_unsubmitted_attempt(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.placement import services
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    _seed_marking_ai(tenant_a)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    teacher_u = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    hod_u = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    lead_u = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        lead = StudentProfileFactory.create(user=lead_u, branch=branch, status=StudentProfile.Status.LEAD)
        test = services.create_test(title="T", created_by=teacher_u, branch=branch)
        services.add_question(test=test, prompt="W?", question_type="writing", points=5)
        services.submit_for_review(test=test)
        test = services.approve_test(test=test, approver=hod_u)
        attempt = services.assign_test(test=test, student=lead, assigned_by=hod_u)  # ASSIGNED, not submitted
    staff = as_user(tenant_a, hod_u)
    r = staff.post(f"{ATTEMPTS}{attempt.id}/mark-writing/", {}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "attempt_not_graded"
