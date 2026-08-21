"""F8-1 - short-answer (typed, auto-graded) placement questions: the answer key is a
list of acceptable answers; a typed response is graded correct if it NORMALIZES (NFC /
casefold / collapse whitespace) to any of them. Covers the vocabulary / fill-in use
case without a human marker."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

ATTEMPTS = "/api/v1/placement/attempts/"


def _setup(tenant, user_in, as_user, *, correct=None, points=2):
    """Build + approve a one-question short_answer test and a prospective lead."""
    from apps.org.tests.factories import BranchFactory
    from apps.placement import services
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    correct = correct if correct is not None else ["blue", "light blue"]

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    builder_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    approver_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    lead_u = user_in(tenant, roles=[Role.STUDENT], branch=branch)
    with schema_context(tenant.schema_name):
        lead = StudentProfileFactory.create(user=lead_u, branch=branch, status=StudentProfile.Status.LEAD)
        test = services.create_test(title="SA placement", created_by=builder_u, branch=branch)
        q = services.add_question(
            test=test,
            prompt="What colour is the sky?",
            question_type="short_answer",
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


def test_exact_answer_scores_full_points(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = _submit(s, aid, "blue").json()["data"]
    assert body["score"] == 2
    assert body["max_score"] == 2
    assert body["level"] == "advanced"


def test_grading_ignores_case_and_surrounding_whitespace(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    assert _submit(s, aid, "  BLUE  ").json()["data"]["score"] == 2


def test_any_acceptable_synonym_is_correct(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    # "Light  Blue" normalizes to "light blue", the second acceptable answer
    assert _submit(s, aid, "Light  Blue").json()["data"]["score"] == 2


def test_grading_matches_across_unicode_form_and_case(tenant_a, user_in, as_user):
    """An accented answer typed in a different keyboard normal form (or case) still
    matches - NFC + casefold - so a correct multilingual answer isn't mis-graded."""
    import unicodedata

    nfd = "café"  # c a f e + U+0301 combining acute => NFD form
    nfc = unicodedata.normalize("NFC", nfd)  # the answer key, composed form
    assert nfc != nfd  # NFC genuinely differs from the NFD source (different code points)
    s = _setup(tenant_a, user_in, as_user, correct=[nfc])
    aid = _assign(s)
    typed = nfd.upper()  # the same word, still decomposed, now uppercased
    assert _submit(s, aid, typed).json()["data"]["score"] == 2


def test_wrong_answer_scores_zero(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = _submit(s, aid, "green").json()["data"]
    assert body["score"] == 0
    assert body["level"] == "beginner"


def test_blank_answer_scores_zero(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    assert _submit(s, aid, "   ").json()["data"]["score"] == 0  # normalizes to "", no match


def test_response_must_be_text(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    r = _submit(s, aid, 42)  # a number, not text
    assert r.status_code == 400
    assert r.json()["code"] == "answer_not_text"


def test_answer_key_is_never_served_to_the_lead(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    aid = _assign(s)
    body = s["lead_c"].get(f"{ATTEMPTS}{aid}/").json()["data"]
    assert body["questions"], "expected the short-answer question to solve"
    assert all("correct_answer" not in q for q in body["questions"])


# --- authoring validation (the key must be a non-empty list of text answers) ---


def _add_bad(tenant, user_in, *, correct):
    from apps.org.tests.factories import BranchFactory
    from apps.placement import services
    from core.exceptions import UnprocessableEntity, ValidationException

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        builder_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
        test = services.create_test(title="t", created_by=builder_u, branch=branch)
        try:
            services.add_question(test=test, prompt="q", question_type="short_answer", correct_answer=correct)
        except (ValidationException, UnprocessableEntity) as exc:
            return exc.code
    return None


def test_authoring_requires_a_list_of_answers(tenant_a, user_in):
    assert _add_bad(tenant_a, user_in, correct="blue") == "answer_not_list"


def test_authoring_rejects_an_empty_answer_list(tenant_a, user_in):
    assert _add_bad(tenant_a, user_in, correct=[]) == "answer_not_list"


def test_authoring_rejects_a_blank_or_non_text_answer(tenant_a, user_in):
    assert _add_bad(tenant_a, user_in, correct=["blue", "  "]) == "invalid_answers"
    assert _add_bad(tenant_a, user_in, correct=["blue", 7]) == "invalid_answers"
