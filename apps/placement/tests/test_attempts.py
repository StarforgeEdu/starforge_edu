"""F1-5 / F1-6 — placement attempts: assign an approved test to a lead, the lead
solves it (never seeing the answer key), and the objective questions are auto-
graded on submit, setting the lead's academic_level instantly.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

ATTEMPTS = "/api/v1/placement/attempts/"


def _approved_test(tenant, branch, builder, approver):
    """Build + approve a 5-objective-point test (2+2 single_choice, 1 true_false)
    plus a writing question. Returns (test, [q1, q2, q3, q4])."""
    from apps.placement import services

    with schema_context(tenant.schema_name):
        test = services.create_test(title="EN placement", created_by=builder, branch=branch)
        q1 = services.add_question(
            test=test,
            prompt="2+2?",
            question_type="single_choice",
            options=["3", "4", "5"],
            correct_answer="4",
            points=2,
        )
        q2 = services.add_question(
            test=test,
            prompt="Capital of France?",
            question_type="single_choice",
            options=["London", "Paris"],
            correct_answer="Paris",
            points=2,
        )
        q3 = services.add_question(
            test=test,
            prompt="Sky is blue?",
            question_type="true_false",
            correct_answer=True,
            points=1,
        )
        q4 = services.add_question(test=test, prompt="Describe your day.", question_type="writing")
        services.submit_for_review(test=test)
        services.approve_test(test=test, approver=approver)
    return test, [q1, q2, q3, q4]


def _setup(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    teacher_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    hod_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    lead_u = user_in(tenant, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant.schema_name):
        lead = StudentProfileFactory.create(user=lead_u, branch=branch, status=StudentProfile.Status.LEAD)
    test, questions = _approved_test(tenant, branch, teacher_u, hod_u)
    return {
        "branch": branch,
        "staff": as_user(tenant, hod_u),
        "teacher_u": teacher_u,
        "lead": lead,
        "lead_u": lead_u,
        "lead_c": as_user(tenant, lead_u),
        "test": test,
        "q": questions,
    }


def _assign(s):
    r = s["staff"].post(ATTEMPTS, {"test": s["test"].id, "student": s["lead"].id}, format="json")
    assert r.status_code == 201, r.content
    return r.json()["data"]["id"]


def _all_correct(s):
    q1, q2, q3, q4 = s["q"]
    return [
        {"question": q1.id, "response": "4"},
        {"question": q2.id, "response": "Paris"},
        {"question": q3.id, "response": True},
        {"question": q4.id, "response": "It was good."},
    ]


def test_assign_solve_and_auto_level(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    # the lead sees the attempt assigned to them
    got = s["lead_c"].get(f"{ATTEMPTS}{aid}/")
    assert got.status_code == 200
    assert got.json()["data"]["status"] == "assigned"

    res = s["lead_c"].post(f"{ATTEMPTS}{aid}/submit/", {"answers": _all_correct(s)}, format="json")
    assert res.status_code == 200, res.content
    body = res.json()["data"]
    assert body["status"] == "graded"
    assert body["score"] == 5  # objective points only (writing excluded from max_score)
    assert body["max_score"] == 5
    assert body["level"] == "advanced"
    # F1-6: the level lands on the lead's profile immediately
    from apps.students.models import StudentProfile

    with schema_context(tenant_a.schema_name):
        assert StudentProfile.objects.get(pk=s["lead"].id).academic_level == "advanced"


def test_answer_key_is_never_served_to_the_lead(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = s["lead_c"].get(f"{ATTEMPTS}{aid}/").json()["data"]
    # the lead gets the questions to solve but NEVER the correct_answer key
    assert body["questions"], "expected questions to solve"
    assert all("correct_answer" not in q for q in body["questions"])


def test_partial_and_failing_scores_map_to_bands(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    q1, q2, q3, _ = s["q"]
    aid = _assign(s)
    # 2/5 objective points -> 40% -> intermediate (only the true_false + one single wrong)
    answers = [
        {"question": q1.id, "response": "3"},  # wrong (0)
        {"question": q2.id, "response": "Paris"},  # right (2)
        {"question": q3.id, "response": False},  # wrong (0)
    ]
    body = s["lead_c"].post(f"{ATTEMPTS}{aid}/submit/", {"answers": answers}, format="json").json()["data"]
    assert body["score"] == 2
    assert body["max_score"] == 5
    assert body["level"] == "intermediate"


def test_writing_question_is_not_auto_graded(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    s["lead_c"].post(f"{ATTEMPTS}{aid}/submit/", {"answers": _all_correct(s)}, format="json")
    # grading detail is staff-only (the lead view hides is_correct — see below)
    body = s["staff"].get(f"{ATTEMPTS}{aid}/").json()["data"]
    writing_qid = s["q"][3].id
    writing_answer = next(a for a in body["answers"] if a["question"] == writing_qid)
    assert writing_answer["is_correct"] is None  # marked by a person later (F8-3)
    assert writing_answer["awarded_points"] == 0


def test_lead_never_sees_per_question_correctness(tenant_a, user_in, as_user):
    """response + is_correct reconstructs the answer key by inference — so the lead
    gets only {question, response}; the full grading is staff-only."""
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = (
        s["lead_c"]
        .post(f"{ATTEMPTS}{aid}/submit/", {"answers": _all_correct(s)}, format="json")
        .json()["data"]
    )
    assert body["answers"], "the lead still sees their own responses"
    assert all("is_correct" not in a and "awarded_points" not in a for a in body["answers"])
    # but a staff member (proctor/manager) sees the full grading
    staff_body = s["staff"].get(f"{ATTEMPTS}{aid}/").json()["data"]
    assert all("is_correct" in a for a in staff_body["answers"])


def test_single_choice_answer_must_be_an_option(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    q1 = s["q"][0]  # single_choice with options 3/4/5
    r = s["lead_c"].post(
        f"{ATTEMPTS}{aid}/submit/", {"answers": [{"question": q1.id, "response": "banana"}]}, format="json"
    )
    assert r.status_code == 400
    assert r.json()["code"] == "answer_not_in_options"


def test_cannot_assign_to_a_non_prospective_student(tenant_a, user_in, as_user):
    """Re-placing an enrolled student would clobber their curated academic_level —
    placement is an intake tool, so only prospective students are eligible."""
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    enrolled_u = user_in(tenant_a, roles=[Role.STUDENT], branch=s["branch"])
    with schema_context(tenant_a.schema_name):
        enrolled = StudentProfileFactory.create(
            user=enrolled_u, branch=s["branch"], status=StudentProfile.Status.ACTIVE
        )
    r = s["staff"].post(ATTEMPTS, {"test": s["test"].id, "student": enrolled.id}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "student_not_prospective"


def test_only_an_approved_test_can_be_assigned(tenant_a, user_in, as_user):
    from apps.placement import services

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        draft = services.create_test(title="draft", created_by=s["teacher_u"], branch=s["branch"])
    r = s["staff"].post(ATTEMPTS, {"test": draft.id, "student": s["lead"].id}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "test_not_approved"


def test_duplicate_assignment_conflicts(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    _assign(s)
    dup = s["staff"].post(ATTEMPTS, {"test": s["test"].id, "student": s["lead"].id}, format="json")
    assert dup.status_code == 409
    assert dup.json()["code"] == "already_assigned"


def test_cannot_submit_twice(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    assert (
        s["lead_c"].post(f"{ATTEMPTS}{aid}/submit/", {"answers": _all_correct(s)}, format="json").status_code
        == 200
    )
    again = s["lead_c"].post(f"{ATTEMPTS}{aid}/submit/", {"answers": _all_correct(s)}, format="json")
    assert again.status_code == 409
    assert again.json()["code"] == "already_submitted"


def test_response_type_is_validated(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    q1, _, q3, _ = s["q"]
    aid = _assign(s)
    # a true/false answered with a string is a clean 400, not a junk row
    bad_tf = s["lead_c"].post(
        f"{ATTEMPTS}{aid}/submit/", {"answers": [{"question": q3.id, "response": "yes"}]}, format="json"
    )
    assert bad_tf.status_code == 400
    assert bad_tf.json()["code"] == "answer_not_boolean"
    # a single_choice answered with a non-string
    bad_sc = s["lead_c"].post(
        f"{ATTEMPTS}{aid}/submit/", {"answers": [{"question": q1.id, "response": 4}]}, format="json"
    )
    assert bad_sc.status_code == 400
    assert bad_sc.json()["code"] == "answer_not_text"


def test_unknown_question_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    r = s["lead_c"].post(
        f"{ATTEMPTS}{aid}/submit/", {"answers": [{"question": 999999, "response": "4"}]}, format="json"
    )
    assert r.status_code == 400
    assert r.json()["code"] == "unknown_question"


def test_boolean_question_id_is_not_accepted_as_integer(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    r = s["lead_c"].post(
        f"{ATTEMPTS}{aid}/submit/", {"answers": [{"question": True, "response": "4"}]}, format="json"
    )
    assert r.status_code == 400
    assert r.json()["code"] == "unknown_question"


def test_another_lead_cannot_see_or_submit(tenant_a, user_in, as_user):
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    other_u = user_in(tenant_a, roles=[Role.STUDENT], branch=s["branch"])
    with schema_context(tenant_a.schema_name):
        StudentProfileFactory.create(user=other_u, branch=s["branch"], status=StudentProfile.Status.LEAD)
    other = as_user(tenant_a, other_u)
    assert other.get(f"{ATTEMPTS}{aid}/").status_code == 404
    assert (
        other.post(f"{ATTEMPTS}{aid}/submit/", {"answers": _all_correct(s)}, format="json").status_code == 404
    )


def test_proctor_can_submit_on_behalf(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    # a placement:write staff member (the assigner's branch) may submit (proctored)
    res = s["staff"].post(f"{ATTEMPTS}{aid}/submit/", {"answers": _all_correct(s)}, format="json")
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "graded"


def test_cannot_assign_a_student_in_another_branch(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        outsider = StudentProfileFactory.create(branch=other_branch, status=StudentProfile.Status.LEAD)
    r = s["staff"].post(ATTEMPTS, {"test": s["test"].id, "student": outsider.id}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "cross_branch"


@pytest.mark.django_db(transaction=True)
def test_submit_works_under_real_autocommit(tenant_a, user_in, as_user):
    from apps.placement import services

    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    with schema_context(tenant_a.schema_name):
        from apps.placement.models import PlacementAttempt

        attempt = PlacementAttempt.objects.get(pk=aid)
        graded = services.submit_attempt(attempt=attempt, answers=_all_correct(s))
    assert graded.status == "graded"
    assert graded.level == "advanced"
