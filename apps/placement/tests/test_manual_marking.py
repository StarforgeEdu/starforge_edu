"""F8-3 (manual path) — a human marker scores placement WRITING answers directly,
without the AI: same recompute as AI marking (set the score, mark is_correct so the
writing counts, recompute the grade + level), but STRICT validation of the person's
input (unknown / duplicate / out-of-range -> clean 4xx, never a silent skip/clamp)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

ATTEMPTS = "/api/v1/placement/attempts/"


def _setup(tenant, user_in, as_user):
    """An APPROVED test (single_choice 2pts + writing 8pts), assigned to a lead who
    submits a correct objective answer + a writing answer -> a GRADED attempt (writing
    unscored, so 2/2 -> advanced at submit)."""
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


def _mark(s, marks):
    return s["staff"].post(
        f"{ATTEMPTS}{s['attempt'].id}/mark-writing-manual/", {"marks": marks}, format="json"
    )


def test_manual_marking_folds_writing_into_the_grade(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    assert s["attempt"].level == "advanced"  # 2/2 objective at submit
    r = _mark(s, [{"question": s["q_write"].id, "score": 4}])
    assert r.status_code == 200, r.content
    body = r.json()["data"]
    assert body["max_score"] == 10  # 2 objective + 8 writing now counted
    assert body["score"] == 6  # 2 + 4
    assert body["level"] == "intermediate"  # 60% -> recomputed down


def test_zero_score_still_counts_in_the_denominator(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    body = _mark(s, [{"question": s["q_write"].id, "score": 0}]).json()["data"]
    assert body["max_score"] == 10  # writing counted at 0, not excluded
    assert body["score"] == 2
    assert body["level"] == "beginner"  # 2/10 = 20%


def test_remarking_overwrites_the_previous_mark(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _mark(s, [{"question": s["q_write"].id, "score": 4}])
    body = _mark(s, [{"question": s["q_write"].id, "score": 7}]).json()["data"]
    assert body["score"] == 9  # 2 + 7 (not double-counted)


def test_score_above_the_questions_points_is_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _mark(s, [{"question": s["q_write"].id, "score": 99}])  # max is 8
    assert r.status_code == 400
    assert r.json()["code"] == "score_out_of_range"


def test_negative_score_is_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _mark(s, [{"question": s["q_write"].id, "score": -1}])
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_score"


def test_unknown_question_is_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _mark(s, [{"question": 999999, "score": 3}])
    assert r.status_code == 400
    assert r.json()["code"] == "unknown_writing_question"


def test_marking_a_non_writing_question_is_rejected(tenant_a, user_in, as_user):
    """The objective (single_choice) question is auto-graded — it can't be hand-marked."""
    s = _setup(tenant_a, user_in, as_user)
    r = _mark(s, [{"question": s["q_obj"].id, "score": 2}])
    assert r.status_code == 400
    assert r.json()["code"] == "unknown_writing_question"


def test_duplicate_mark_is_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _mark(s, [{"question": s["q_write"].id, "score": 3}, {"question": s["q_write"].id, "score": 4}])
    assert r.status_code == 400
    assert r.json()["code"] == "duplicate_mark"


def test_lead_cannot_mark_their_own_writing(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = s["lead_c"].post(
        f"{ATTEMPTS}{s['attempt'].id}/mark-writing-manual/",
        {"marks": [{"question": s["q_write"].id, "score": 8}]},
        format="json",
    )
    assert r.status_code == 403  # the lead holds no placement:write


def test_cannot_mark_an_unsubmitted_attempt(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.placement import services
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    teacher_u = user_in(tenant_a, roles=[Role.TEACHER], branch=branch)
    hod_u = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
    lead_u = user_in(tenant_a, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant_a.schema_name):
        lead = StudentProfileFactory.create(user=lead_u, branch=branch, status=StudentProfile.Status.LEAD)
        test = services.create_test(title="T", created_by=teacher_u, branch=branch)
        q_write = services.add_question(test=test, prompt="W?", question_type="writing", points=5)
        services.submit_for_review(test=test)
        test = services.approve_test(test=test, approver=hod_u)
        attempt = services.assign_test(test=test, student=lead, assigned_by=hod_u)  # ASSIGNED, not submitted
    staff = as_user(tenant_a, hod_u)
    r = staff.post(
        f"{ATTEMPTS}{attempt.id}/mark-writing-manual/",
        {"marks": [{"question": q_write.id, "score": 3}]},
        format="json",
    )
    assert r.status_code == 422
    assert r.json()["code"] == "attempt_not_graded"


def test_empty_marks_list_is_rejected_by_the_serializer(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _mark(s, [])
    assert r.status_code == 400
