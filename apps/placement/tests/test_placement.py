"""F1-2 / F1-4 — placement test bank + approval lifecycle.

A builder authors a test out of questions while DRAFT, submits it for review, and
a *different* manager approves it (maker-checker). Tests cover the lifecycle, the
self-approval block, question validation, draft-only editing, branch scoping, and
the approve transition under real autocommit (select_for_update guard).
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TESTS = "/api/v1/placement/tests/"


def _setup(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    teacher_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)
    hod1_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    hod2_u = user_in(tenant, roles=[Role.HEAD_OF_DEPT], branch=branch)
    return {
        "branch": branch,
        "teacher_u": teacher_u,
        "teacher": as_user(tenant, teacher_u),
        "hod1_u": hod1_u,
        "hod1": as_user(tenant, hod1_u),
        "hod2_u": hod2_u,
        "hod2": as_user(tenant, hod2_u),
    }


def _q(**over):
    body = {
        "prompt": "What is 2 + 2?",
        "question_type": "single_choice",
        "options": ["3", "4", "5"],
        "correct_answer": "4",
        "points": 2,
    }
    body.update(over)
    return body


def _build_pending(client, branch_id):
    """Create a test, add a question, submit it -> returns the PENDING test id."""
    tid = client.post(TESTS, {"title": "English placement", "branch": branch_id}, format="json").json()["data"]["id"]
    assert client.post(f"{TESTS}{tid}/questions/", _q(), format="json").status_code == 201
    submitted = client.post(f"{TESTS}{tid}/submit/", {}, format="json")
    assert submitted.status_code == 200, submitted.content
    assert submitted.json()["data"]["status"] == "pending"
    return tid


def test_build_submit_approve_lifecycle(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = _build_pending(s["teacher"], s["branch"].id)
    # a manager (different person) approves -> live
    approved = s["hod1"].post(f"{TESTS}{tid}/approve/", {}, format="json")
    assert approved.status_code == 200, approved.content
    assert approved.json()["data"]["status"] == "approved"
    assert approved.json()["data"]["approved_by"] == s["hod1_u"].id


def test_builder_cannot_approve_own_test(tenant_a, user_in, as_user):
    """Maker-checker: a manager who built the test cannot approve it themselves."""
    s = _setup(tenant_a, user_in, as_user)
    tid = _build_pending(s["hod1"], s["branch"].id)  # hod1 builds AND submits
    own = s["hod1"].post(f"{TESTS}{tid}/approve/", {}, format="json")
    assert own.status_code == 403
    assert own.json()["code"] == "self_approval"
    # a different manager can approve it
    assert s["hod2"].post(f"{TESTS}{tid}/approve/", {}, format="json").status_code == 200


def test_teacher_cannot_approve(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = _build_pending(s["teacher"], s["branch"].id)
    # a teacher holds placement:write but not placement:approve
    assert s["teacher"].post(f"{TESTS}{tid}/approve/", {}, format="json").status_code == 403


def test_cannot_approve_a_draft(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    s["teacher"].post(f"{TESTS}{tid}/questions/", _q(), format="json")
    # never submitted -> still DRAFT -> not approvable
    r = s["hod1"].post(f"{TESTS}{tid}/approve/", {}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "test_not_pending"


def test_cannot_submit_an_empty_test(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    r = s["teacher"].post(f"{TESTS}{tid}/submit/", {}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "test_has_no_questions"


def test_cannot_edit_after_submit(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = _build_pending(s["teacher"], s["branch"].id)
    # PENDING is frozen — no more questions
    r = s["teacher"].post(f"{TESTS}{tid}/questions/", _q(prompt="late"), format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "test_not_draft"


def test_reject_kicks_back_to_draft_then_editable(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = _build_pending(s["teacher"], s["branch"].id)
    rejected = s["hod1"].post(f"{TESTS}{tid}/reject/", {"reason": "Add a writing task"}, format="json")
    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == "draft"
    assert rejected.json()["data"]["reject_reason"] == "Add a writing task"
    # back to DRAFT -> the builder can edit + resubmit
    assert s["teacher"].post(f"{TESTS}{tid}/questions/", _q(prompt="more"), format="json").status_code == 201
    assert s["teacher"].post(f"{TESTS}{tid}/submit/", {}, format="json").status_code == 200


def test_reject_needs_a_reason(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = _build_pending(s["teacher"], s["branch"].id)
    assert s["hod1"].post(f"{TESTS}{tid}/reject/", {"reason": "   "}, format="json").status_code == 400


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        ({"options": ["only-one"]}, "choice_needs_options"),
        ({"options": ["A", "A"]}, "duplicate_options"),
        ({"correct_answer": "Z"}, "answer_not_in_options"),
        ({"options": 7}, "invalid_options"),  # scalar would 500 on len()
        ({"options": "ABCD"}, "invalid_options"),  # string would "match" by substring
    ],
)
def test_single_choice_validation(tenant_a, user_in, as_user, bad, code):
    s = _setup(tenant_a, user_in, as_user)
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    r = s["teacher"].post(f"{TESTS}{tid}/questions/", _q(**bad), format="json")
    assert r.status_code == 400
    assert r.json()["code"] == code


def test_true_false_and_writing_validation(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    # true/false needs a boolean key
    bad_tf = s["teacher"].post(
        f"{TESTS}{tid}/questions/",
        {"prompt": "Sky is blue?", "question_type": "true_false", "correct_answer": "yes"},
        format="json",
    )
    assert bad_tf.status_code == 400
    assert bad_tf.json()["code"] == "answer_not_boolean"
    # a writing question must NOT carry an answer key
    bad_w = s["teacher"].post(
        f"{TESTS}{tid}/questions/",
        {"prompt": "Describe your day.", "question_type": "writing", "correct_answer": "x"},
        format="json",
    )
    assert bad_w.status_code == 400
    assert bad_w.json()["code"] == "writing_has_no_answer"
    # the valid forms succeed
    assert s["teacher"].post(
        f"{TESTS}{tid}/questions/",
        {"prompt": "Sky is blue?", "question_type": "true_false", "correct_answer": True},
        format="json",
    ).status_code == 201
    assert s["teacher"].post(
        f"{TESTS}{tid}/questions/",
        {"prompt": "Describe your day.", "question_type": "writing"},
        format="json",
    ).status_code == 201


def test_remove_question_while_draft(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    qid = s["teacher"].post(f"{TESTS}{tid}/questions/", _q(), format="json").json()["data"]["id"]
    assert s["teacher"].post(f"{TESTS}{tid}/questions/{qid}/remove/", {}, format="json").status_code == 204
    # the test now has no questions
    assert s["teacher"].get(f"{TESTS}{tid}/", format="json").json()["data"]["questions"] == []


def test_branch_scoping_on_create_and_read(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other = BranchFactory.create()
    # cannot build a test for a branch you're not in
    cross = s["teacher"].post(TESTS, {"title": "X", "branch": other.id}, format="json")
    assert cross.status_code == 403
    assert cross.json()["code"] == "cross_branch"
    # a manager in `other` branch does not see the home-branch test
    tid = _build_pending(s["teacher"], s["branch"].id)
    outsider = as_user(tenant_a, user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=other))
    assert outsider.get(f"{TESTS}{tid}/", format="json").status_code == 404


def test_student_has_no_placement_access(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    student = as_user(tenant_a, user_in(tenant_a, roles=[Role.STUDENT], branch=s["branch"]))
    assert student.post(TESTS, {"title": "T"}, format="json").status_code == 403
    # and even read is walled off (placement is staff-only until F1-5 assigns attempts)
    assert student.get(TESTS, format="json").status_code == 403


def test_draft_test_can_be_deleted(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    tid = s["teacher"].post(TESTS, {"title": "T", "branch": s["branch"].id}, format="json").json()["data"]["id"]
    s["teacher"].post(f"{TESTS}{tid}/questions/", _q(), format="json")
    assert s["teacher"].delete(f"{TESTS}{tid}/").status_code == 204
    assert s["teacher"].get(f"{TESTS}{tid}/").status_code == 404


def test_pending_and_approved_tests_cannot_be_deleted(tenant_a, user_in, as_user):
    """A pending/approved test carries the lifecycle + the checker's sign-off — a
    single builder must NOT be able to hard-delete it (maker-checker accountability)."""
    s = _setup(tenant_a, user_in, as_user)
    tid = _build_pending(s["teacher"], s["branch"].id)
    # PENDING: the builder cannot yank it out of the review queue
    rejected_del = s["teacher"].delete(f"{TESTS}{tid}/")
    assert rejected_del.status_code == 422
    assert rejected_del.json()["code"] == "test_not_draft"
    # APPROVED: still frozen against deletion
    s["hod1"].post(f"{TESTS}{tid}/approve/", {}, format="json")
    assert s["teacher"].delete(f"{TESTS}{tid}/").status_code == 422
    # ...and the manager can't unilaterally erase their own approved artifact either
    assert s["hod1"].delete(f"{TESTS}{tid}/").status_code == 422


@pytest.mark.django_db(transaction=True)
def test_approve_works_under_real_autocommit(tenant_a, user_in):
    """approve_test uses select_for_update, which needs an explicit
    @transaction.atomic — exercise the REAL autocommit path so a missing
    decorator would 500 rather than pass under the ambient test transaction."""
    from apps.placement import services

    builder = user_in(tenant_a, roles=[Role.TEACHER])
    approver = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT])
    with schema_context(tenant_a.schema_name):
        test = services.create_test(title="T", created_by=builder)
        services.add_question(
            test=test,
            prompt="2+2?",
            question_type="single_choice",
            options=["3", "4"],
            correct_answer="4",
        )
        services.submit_for_review(test=test)
        approved = services.approve_test(test=test, approver=approver)
    assert approved.status == "approved"
