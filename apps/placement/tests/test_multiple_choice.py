"""F8-1 — multiple-choice (multi-select) placement questions: the answer key is a
SUBSET of the options and grading is all-or-nothing (the chosen set must equal the
correct set exactly). A new objective type alongside single_choice / true_false.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

ATTEMPTS = "/api/v1/placement/attempts/"


def _setup(tenant, user_in, as_user, *, options=None, correct=None, points=2):
    """Build + approve a one-question multiple_choice test and a prospective lead."""
    from apps.org.tests.factories import BranchFactory
    from apps.placement import services
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    options = options if options is not None else ["1", "2", "3", "4"]
    correct = correct if correct is not None else ["2", "4"]

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    builder_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    approver_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    lead_u = user_in(tenant, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant.schema_name):
        lead = StudentProfileFactory.create(user=lead_u, branch=branch, status=StudentProfile.Status.LEAD)
        test = services.create_test(title="MC placement", created_by=builder_u, branch=branch)
        q = services.add_question(
            test=test,
            prompt="Select the even numbers",
            question_type="multiple_choice",
            options=options,
            correct_answer=correct,
            points=points,
        )
        services.submit_for_review(test=test)
        services.approve_test(test=test, approver=approver_u)
    return {
        "branch": branch,
        "staff": as_user(tenant, approver_u),
        "lead": lead,
        "lead_c": as_user(tenant, lead_u),
        "test": test,
        "q": q,
    }


def _assign(s):
    r = s["staff"].post(ATTEMPTS, {"test": s["test"].id, "student": s["lead"].id}, format="json")
    assert r.status_code == 201, r.content
    return r.json()["data"]["id"]


def _submit(s, aid, response):
    return s["lead_c"].post(
        f"{ATTEMPTS}{aid}/submit/",
        {"answers": [{"question": s["q"].id, "response": response}]},
        format="json",
    )


def test_exact_set_scores_full_points(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = _submit(s, aid, ["2", "4"]).json()["data"]
    assert body["status"] == "graded"
    assert body["score"] == 2
    assert body["max_score"] == 2
    assert body["level"] == "advanced"  # 100%


def test_grading_is_order_independent(tenant_a, user_in, as_user):
    """Selecting the same options in a different order is still fully correct —
    the chosen SET is what matters, not the sequence."""
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = _submit(s, aid, ["4", "2"]).json()["data"]
    assert body["score"] == 2


def test_partial_selection_scores_zero(tenant_a, user_in, as_user):
    """All-or-nothing: a correct-but-incomplete set earns no points (a transparent
    rule, not silent partial credit)."""
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = _submit(s, aid, ["2"]).json()["data"]
    assert body["score"] == 0
    assert body["max_score"] == 2
    assert body["level"] == "beginner"  # 0%


def test_over_selection_scores_zero(tenant_a, user_in, as_user):
    """Adding a wrong option to the correct ones is still wrong (no credit for
    shotgunning every option)."""
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = _submit(s, aid, ["2", "4", "1"]).json()["data"]
    assert body["score"] == 0


def test_empty_selection_is_left_blank_and_scores_zero(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = _submit(s, aid, []).json()["data"]
    assert body["score"] == 0
    assert body["max_score"] == 2


def test_response_must_be_a_list(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    r = _submit(s, aid, "2")  # a bare string, not a list
    assert r.status_code == 400
    assert r.json()["code"] == "answer_not_list"


def test_response_options_must_be_offered(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    r = _submit(s, aid, ["2", "9"])  # "9" was never an option
    assert r.status_code == 400
    assert r.json()["code"] == "answer_not_in_options"


def test_answer_key_is_never_served_to_the_lead(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = s["lead_c"].get(f"{ATTEMPTS}{aid}/").json()["data"]
    assert body["questions"], "expected the multiple-choice question to solve"
    assert all("correct_answer" not in q for q in body["questions"])


# --- authoring validation (the answer key must be a coherent subset of the options) ---


def _add_bad(tenant, user_in, *, options, correct):
    from apps.org.tests.factories import BranchFactory
    from apps.placement import services
    from core.exceptions import UnprocessableEntity, ValidationException

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        builder_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
        test = services.create_test(title="t", created_by=builder_u, branch=branch)
        try:
            services.add_question(
                test=test,
                prompt="q",
                question_type="multiple_choice",
                options=options,
                correct_answer=correct,
            )
        except (ValidationException, UnprocessableEntity) as exc:
            return exc.code
    return None


def test_authoring_requires_a_list_answer(tenant_a, user_in):
    assert _add_bad(tenant_a, user_in, options=["a", "b"], correct="a") == "answer_not_list"


def test_authoring_rejects_an_empty_answer(tenant_a, user_in):
    assert _add_bad(tenant_a, user_in, options=["a", "b"], correct=[]) == "answer_not_list"


def test_authoring_answer_must_be_among_options(tenant_a, user_in):
    assert _add_bad(tenant_a, user_in, options=["a", "b"], correct=["a", "z"]) == "answer_not_in_options"


def test_authoring_rejects_duplicate_answers(tenant_a, user_in):
    assert _add_bad(tenant_a, user_in, options=["a", "b"], correct=["a", "a"]) == "duplicate_answers"


def test_authoring_needs_at_least_two_options(tenant_a, user_in):
    assert _add_bad(tenant_a, user_in, options=["a"], correct=["a"]) == "choice_needs_options"


def test_authoring_rejects_unhashable_junk_answer(tenant_a, user_in):
    """A list/dict entry in the key (which the AI path could emit) is rejected as a
    clean 422, never a TypeError 500 from set()."""
    assert _add_bad(tenant_a, user_in, options=["a", "b"], correct=[["a"]]) == "answer_not_in_options"
