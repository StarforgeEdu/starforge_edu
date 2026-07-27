"""Assignments lane tests (D2-D): late-flag grace, resubmit limit, rubric
validation, draft/cross-cohort scoping, due-soon idempotency, the four signals,
upload-url allowlist + key prefix, plagiarism stub, cross-tenant, query budgets."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import time_machine
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.assignments import selectors, services
from apps.assignments.models import Assignment
from apps.assignments.services import PlagiarismResult
from apps.assignments.signals import (
    ai_feedback_requested,
    assignment_due_soon,
    assignment_published,
    submission_graded,
)
from apps.assignments.tests.factories import AssignmentFactory
from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
from apps.org.models import CenterSettings
from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.teachers.tests.factories import TeacherProfileFactory
from core.exceptions import ConflictException, UnprocessableEntity

pytestmark = pytest.mark.django_db


def _aware(y, m, d, hh, mm=0):
    return timezone.make_aware(datetime(y, m, d, hh, mm))


def test_ai_placeholder_grade_is_not_presented_as_a_score():
    """R5/CONF4: the auto AI-feedback pipeline creates a SubmissionGrade(score=0,
    graded_by=None) on every submission just to carry ai_feedback. The presenter must
    NOT show that placeholder as an official 0.00 mark — a student would see a fake 0
    for work the teacher never graded. A real human 0 (graded_by set) is still shown."""
    from apps.assignments.models import SubmissionGrade
    from apps.assignments.presenters import grade_to_dict

    placeholder = SubmissionGrade(
        submission_id=1, score=Decimal("0"), graded_by_id=None, ai_feedback="Nice intro", feedback=""
    )
    d = grade_to_dict(placeholder)
    assert d["score"] is None  # not a real mark
    assert d["graded"] is False
    assert d["ai_feedback"] == "Nice intro"  # advisory feedback still surfaces

    human_zero = SubmissionGrade(
        submission_id=1, score=Decimal("0"), graded_by_id=42, ai_feedback="", feedback="redo"
    )
    d2 = grade_to_dict(human_zero)
    assert d2["score"] == "0.00"  # a teacher's real 0 stands
    assert d2["graded"] is True


def _set_knob(**kwargs) -> None:
    from django.core.cache import cache

    settings = CenterSettings.load()
    for key, value in kwargs.items():
        setattr(settings, key, value)
    settings.save(update_fields=list(kwargs))
    cache.clear()


def _member(cohort, branch) -> Any:
    student = StudentProfileFactory(branch=branch)
    CohortMembershipFactory(cohort=cohort, student=student)
    return student


# --------------------------------------------------------------------------- #
# late flag + resubmit limit (knob-driven)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("offset_min", "expected_late"), [(29, False), (30, False), (31, True)])
def test_late_flag_boundaries_with_grace(tenant_a, offset_min, expected_late):
    with schema_context(tenant_a.schema_name):
        _set_knob(assignment_grace_minutes=30)
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        due = _aware(2026, 6, 1, 12)
        assignment: Any = AssignmentFactory(cohort=cohort, due_at=due)
        student = _member(cohort, branch)
        with time_machine.travel(due + timedelta(minutes=offset_min), tick=False):
            submission = services.submit(assignment=assignment, student=student)
        assert submission.is_late is expected_late


def test_resubmit_limit_default_and_per_assignment_override(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)

        # Default knob = 2 → 3 attempts allowed, 4th rejected.
        default_assignment: Any = AssignmentFactory(cohort=cohort)
        student = _member(cohort, branch)
        for _ in range(3):
            services.submit(assignment=default_assignment, student=student)
        with pytest.raises(UnprocessableEntity) as exc:
            services.submit(assignment=default_assignment, student=student)
        assert exc.value.code == "resubmit_limit_exceeded"

        # Per-assignment override = 0 → only the original attempt.
        strict: Any = AssignmentFactory(cohort=cohort, max_resubmits=0)
        other = _member(cohort, branch)
        services.submit(assignment=strict, student=other)
        with pytest.raises(UnprocessableEntity) as exc2:
            services.submit(assignment=strict, student=other)
        assert exc2.value.code == "resubmit_limit_exceeded"


def test_submit_rejects_closed_and_non_member(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        closed: Any = AssignmentFactory(cohort=cohort, status=Assignment.Status.CLOSED)
        member = _member(cohort, branch)
        with pytest.raises(UnprocessableEntity) as exc:
            services.submit(assignment=closed, student=member)
        assert exc.value.code == "assignment_closed"

        published: Any = AssignmentFactory(cohort=cohort)
        outsider: Any = StudentProfileFactory(branch=branch)  # no membership
        with pytest.raises(UnprocessableEntity) as exc2:
            services.submit(assignment=published, student=outsider)
        assert exc2.value.code == "student_not_in_cohort"


def test_publish_does_not_reopen_closed_assignment(tenant_a):
    """D2-D review: publish must only transition DRAFT->PUBLISHED. A CLOSED
    assignment must NOT silently reopen + re-emit assignment_published."""
    received: list[int] = []

    def _recv(sender, assignment_id, **kw):
        received.append(assignment_id)

    assignment_published.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            cohort = CohortFactory(branch=branch)
            closed: Any = AssignmentFactory(cohort=cohort, status=Assignment.Status.CLOSED)
            with pytest.raises(UnprocessableEntity) as exc:
                services.publish_assignment(assignment=closed)
            assert exc.value.code == "assignment_not_draft"
            closed.refresh_from_db()
            assert closed.status == Assignment.Status.CLOSED  # unchanged
        assert received == []  # no re-notify
    finally:
        assignment_published.disconnect(_recv)


def test_publish_already_published_is_noop(tenant_a):
    """Re-publishing a PUBLISHED assignment is a silent no-op (no re-emit)."""
    received: list[int] = []

    def _recv(sender, assignment_id, **kw):
        received.append(assignment_id)

    assignment_published.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            cohort = CohortFactory(branch=branch)
            published: Any = AssignmentFactory(cohort=cohort, status=Assignment.Status.PUBLISHED)
            result = services.publish_assignment(assignment=published)
            assert result.status == Assignment.Status.PUBLISHED
        assert received == []
    finally:
        assignment_published.disconnect(_recv)


def test_submit_rejects_foreign_tenant_attachment_key(tenant_a):
    """D2-D review: attachment_keys must be under this tenant's prefix; a key
    shaped like another tenant's path is rejected with a stable 422 code."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = _member(cohort, branch)
        assignment: Any = AssignmentFactory(cohort=cohort)

        # A key under another tenant's prefix.
        with pytest.raises(UnprocessableEntity) as exc:
            services.submit(
                assignment=assignment,
                student=student,
                attachment_keys=["tenant_b/assignments/abc/essay.pdf"],
            )
        assert exc.value.code == "invalid_attachment_key"

        # A correctly-prefixed key for this tenant is accepted.
        good_key = f"{tenant_a.schema_name}/assignments/{'a' * 32}/essay.pdf"
        sub = services.submit(assignment=assignment, student=student, attachment_keys=[good_key])
        assert sub.attachments == [good_key]


def test_concurrent_submit_integrity_error_is_clean_conflict(tenant_a, monkeypatch):
    """D2-D review: a unique-constraint collision on (assignment, student,
    attempt_number) must surface a clean 409 ConflictException, not a 500."""
    from django.db import IntegrityError

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = _member(cohort, branch)
        assignment: Any = AssignmentFactory(cohort=cohort)

        # Simulate the loser of a race: create() raises IntegrityError.
        from apps.assignments.models import Submission

        def _boom(*args, **kwargs):
            raise IntegrityError("duplicate key value violates unique constraint")

        monkeypatch.setattr(Submission.objects, "create", _boom)
        with pytest.raises(ConflictException) as exc:
            services.submit(assignment=assignment, student=student)
        assert exc.value.code == "submission_conflict"
        assert exc.value.status_code == 409


# --------------------------------------------------------------------------- #
# rubric validation
# --------------------------------------------------------------------------- #


def test_rubric_validation_unknown_criterion_and_sum_cap(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = _member(cohort, branch)

        ok_rubric: Any = AssignmentFactory(
            cohort=cohort,
            max_score=Decimal("100"),
            rubric=[{"criterion": "clarity", "max_points": 50}, {"criterion": "depth", "max_points": 50}],
        )
        sub = services.submit(assignment=ok_rubric, student=student)
        with pytest.raises(UnprocessableEntity) as exc:
            services.grade_submission(
                submission=sub, score=Decimal("80"), rubric_scores=[{"criterion": "nope", "points": 10}]
            )
        assert exc.value.code == "unknown_rubric_criterion"

        # Rubric whose Σ max_points exceeds the assignment max_score.
        over: Any = AssignmentFactory(
            cohort=cohort,
            max_score=Decimal("100"),
            rubric=[{"criterion": "a", "max_points": 70}, {"criterion": "b", "max_points": 70}],
        )
        sub2 = services.submit(assignment=over, student=_member(cohort, branch))
        with pytest.raises(UnprocessableEntity) as exc2:
            services.grade_submission(submission=sub2, score=Decimal("90"), rubric_scores=[])
        assert exc2.value.code == "rubric_exceeds_max_score"


def test_rubric_sum_cap_rejected_at_create(tenant_a, user_in, as_user):
    """D2-D review: a rubric whose Σ max_points exceed max_score must be rejected
    at authoring time (422), not only when a teacher later tries to grade."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        cohort: Any = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        cohort_id = cohort.id

    resp = as_user(tenant_a, teacher_user).post(
        "/api/v1/assignments/",
        {
            "cohort": cohort_id,
            "title": "Bad rubric",
            "due_at": (timezone.now() + timedelta(days=3)).isoformat(),
            "max_score": "100",
            "rubric": [{"criterion": "a", "max_points": 70}, {"criterion": "b", "max_points": 70}],
        },
        format="json",
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "rubric_exceeds_max_score"


def test_grade_score_out_of_range(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = _member(cohort, branch)
        assignment: Any = AssignmentFactory(cohort=cohort, max_score=Decimal("100"))
        sub = services.submit(assignment=assignment, student=student)
        with pytest.raises(UnprocessableEntity) as exc:
            services.grade_submission(submission=sub, score=Decimal("101"))
        assert exc.value.code == "score_out_of_range"


def test_grade_rejects_malformed_rubric_scores_400_not_500(tenant_a, user_in, as_user):
    """A rubric_scores element that isn't an object with a string criterion must be a
    clean 400 (the old ListField(child=DictField())), never a 500 (a non-dict -> the
    domain fn's rs.get() AttributeError; an unhashable criterion -> TypeError)."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        cohort = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        assignment: Any = AssignmentFactory(
            cohort=cohort, status=Assignment.Status.PUBLISHED, max_score=Decimal("100")
        )
        sub = services.submit(assignment=assignment, student=_member(cohort, branch))
        sub_id = sub.id

    client = as_user(tenant_a, teacher_user)
    for bad in (["foo"], [123], [{"criterion": [1, 2]}]):
        r = client.post(
            f"/api/v1/assignments/submissions/{sub_id}/grade/",
            {"score": "50", "rubric_scores": bad},
            format="json",
        )
        assert r.status_code == 400, (bad, r.status_code, r.content)


def test_patch_max_score_null_is_400_not_conflict(tenant_a, user_in, as_user):
    """PATCH max_score: null must 400 (the model column is NOT NULL) — the old serializer
    rejected it as a validation error, not a 409/500."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        cohort = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        assignment: Any = AssignmentFactory(cohort=cohort, status=Assignment.Status.DRAFT)
        aid = assignment.id

    r = as_user(tenant_a, teacher_user).patch(
        f"/api/v1/assignments/{aid}/", {"max_score": None}, format="json"
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# plagiarism stub
# --------------------------------------------------------------------------- #


def test_plagiarism_stub_typed(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = _member(cohort, branch)
        assignment: Any = AssignmentFactory(cohort=cohort)
        sub = services.submit(assignment=assignment, student=student)
        result = services.check_submission(sub)
        assert isinstance(result, PlagiarismResult)
        assert (result.status, result.score) == ("not_implemented", None)


# --------------------------------------------------------------------------- #
# upload-url
# --------------------------------------------------------------------------- #


def test_upload_url_key_prefix_and_allowlist(tenant_a, monkeypatch):
    monkeypatch.setattr(services, "presign_upload", lambda key, content_type="": f"https://s3/{key}")
    with schema_context(tenant_a.schema_name):
        result = services.validate_and_presign_upload(
            filename="essay.pdf", content_type="application/pdf", size_bytes=1024
        )
        assert result["key"].startswith(f"{tenant_a.schema_name}/assignments/")
        assert result["key"].endswith("/essay.pdf")

        # D2-D review: a filename with path separators / traversal must be
        # sanitized to its basename so it cannot escape the {uuid}/ isolation.
        traversal = services.validate_and_presign_upload(
            filename="../../etc/passwd.pdf", content_type="application/pdf", size_bytes=1024
        )
        assert traversal["key"].startswith(f"{tenant_a.schema_name}/assignments/")
        assert traversal["key"].endswith("/passwd.pdf")
        assert ".." not in traversal["key"]

        windows = services.validate_and_presign_upload(
            filename="sub\\dir\\report.pdf", content_type="application/pdf", size_bytes=1024
        )
        assert windows["key"].endswith("/report.pdf")
        assert "\\" not in windows["key"]

        with pytest.raises(UnprocessableEntity) as exc:
            services.validate_and_presign_upload(
                filename="virus.exe", content_type="application/octet-stream", size_bytes=10
            )
        assert exc.value.code == "file_type_not_allowed"

        with pytest.raises(UnprocessableEntity) as exc2:
            services.validate_and_presign_upload(
                filename="big.pdf", content_type="application/pdf", size_bytes=10**12
            )
        assert exc2.value.code == "file_too_large"


# --------------------------------------------------------------------------- #
# due-soon task + signals
# --------------------------------------------------------------------------- #


def test_due_soon_task_idempotent(tenant_a):
    received: list[int] = []

    def _recv(sender, assignment_id, **kw):
        received.append(assignment_id)

    assignment_due_soon.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            cohort = CohortFactory(branch=branch)
            AssignmentFactory(cohort=cohort, due_at=timezone.now() + timedelta(hours=12))
            assert services.emit_due_soon_reminders() == 1
            assert services.emit_due_soon_reminders() == 0  # stamped → idempotent
        assert len(received) == 1
    finally:
        assignment_due_soon.disconnect(_recv)


def test_all_four_signals_emitted(tenant_a, user_in, django_capture_on_commit_callbacks):
    seen: set[str] = set()
    receivers = {
        "published": (assignment_published, lambda **k: seen.add("published")),
        "due_soon": (assignment_due_soon, lambda **k: seen.add("due_soon")),
        "graded": (submission_graded, lambda **k: seen.add("graded")),
        "ai": (ai_feedback_requested, lambda **k: seen.add("ai")),
    }
    for signal, fn in receivers.values():
        signal.connect(fn)
    try:
        teacher_user = user_in(tenant_a, roles=["teacher"])
        with schema_context(tenant_a.schema_name):
            branch = BranchFactory()
            teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
            cohort = CohortFactory(branch=branch, primary_teacher=teacher_profile)
            student = _member(cohort, branch)

            draft: Any = AssignmentFactory(
                cohort=cohort, status=Assignment.Status.DRAFT, due_at=timezone.now() + timedelta(hours=10)
            )
            with django_capture_on_commit_callbacks(execute=True):
                services.publish_assignment(assignment=draft, actor=teacher_user)
            services.emit_due_soon_reminders()

            sub = services.submit(assignment=draft, student=student)
            with django_capture_on_commit_callbacks(execute=True):
                services.grade_submission(submission=sub, score=Decimal("90"))
            services.request_ai_feedback(submission=sub, requested_by=teacher_user)

        assert seen == {"published", "due_soon", "graded", "ai"}
    finally:
        for signal, fn in receivers.values():
            signal.disconnect(fn)


# --------------------------------------------------------------------------- #
# scoping (drafts, cross-cohort) via the API
# --------------------------------------------------------------------------- #


def test_draft_invisible_to_students(tenant_a, user_in, as_user):
    student_user = user_in(tenant_a, roles=["student"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = StudentProfileFactory(user=student_user, branch=branch)
        CohortMembershipFactory(cohort=cohort, student=student)
        published: Any = AssignmentFactory(cohort=cohort, status=Assignment.Status.PUBLISHED)
        draft: Any = AssignmentFactory(cohort=cohort, status=Assignment.Status.DRAFT)
        published_id, draft_id = published.id, draft.id

    client = as_user(tenant_a, student_user)
    body = client.get("/api/v1/assignments/").json()
    assert {a["id"] for a in body["data"]} == {published_id}
    assert client.get(f"/api/v1/assignments/{draft_id}/").status_code == 404


def test_cross_cohort_submit_404(tenant_a, user_in, as_user):
    student_user = user_in(tenant_a, roles=["student"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        my_cohort = CohortFactory(branch=branch)
        other_cohort = CohortFactory(branch=branch)
        student = StudentProfileFactory(user=student_user, branch=branch)
        CohortMembershipFactory(cohort=my_cohort, student=student)
        foreign: Any = AssignmentFactory(cohort=other_cohort, status=Assignment.Status.PUBLISHED)
        foreign_id = foreign.id

    resp = as_user(tenant_a, student_user).post(
        f"/api/v1/assignments/{foreign_id}/submissions/", {"text": "hi"}, format="json"
    )
    assert resp.status_code == 404  # scoped out, not a 403 existence leak


def test_student_submits_own_cohort_201(tenant_a, user_in, as_user):
    student_user = user_in(tenant_a, roles=["student"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = StudentProfileFactory(user=student_user, branch=branch)
        CohortMembershipFactory(cohort=cohort, student=student)
        assignment: Any = AssignmentFactory(cohort=cohort, status=Assignment.Status.PUBLISHED)
        assignment_id = assignment.id

    resp = as_user(tenant_a, student_user).post(
        f"/api/v1/assignments/{assignment_id}/submissions/", {"text": "my answer"}, format="json"
    )
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["attempt_number"] == 1
    assert body["status"] == "submitted"
    # Self-describing submission (owner: "when was it due / was it late / which attempt").
    assert body["student_name"] == student_user.get_full_name()
    assert body["assignment_title"]  # the assignment's title travels with the submission
    assert "assignment_due_at" in body
    assert body["is_late"] is False


def test_assignment_create_requires_write(tenant_a, as_role):
    from core.permissions import Role

    client, _ = as_role(Role.STUDENT)
    resp = client.post("/api/v1/assignments/", {}, format="json")
    assert resp.status_code == 403


def test_teacher_cannot_create_assignment_in_non_taught_cohort(tenant_a, user_in, as_user):
    """Write-path scoping: a teacher with assignments:write may NOT POST an
    assignment into a cohort they don't teach (reads were scoped, writes were
    not). The out-of-scope cohort PK is filtered from the serializer queryset, so
    it 400s; the teacher's own cohort succeeds (201)."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        own_cohort: Any = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        foreign_cohort: Any = CohortFactory(branch=branch)  # taught by nobody / another teacher
        own_id, foreign_id = own_cohort.id, foreign_cohort.id

    client = as_user(tenant_a, teacher_user)
    base = {
        "title": "Essay",
        "due_at": (timezone.now() + timedelta(days=3)).isoformat(),
        "max_score": "100",
    }

    foreign = client.post("/api/v1/assignments/", {**base, "cohort": foreign_id}, format="json")
    assert foreign.status_code == 400  # cohort not in the teacher's writable queryset

    ok = client.post("/api/v1/assignments/", {**base, "cohort": own_id}, format="json")
    assert ok.status_code == 201
    assert ok.json()["data"]["cohort"] == own_id


def test_teacher_cannot_repoint_assignment_to_non_taught_cohort(tenant_a, user_in, as_user):
    """PATCH is scoped too: a teacher cannot move an owned assignment's cohort to a
    cohort they don't teach."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        own_cohort: Any = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        foreign_cohort: Any = CohortFactory(branch=branch)
        assignment: Any = AssignmentFactory(cohort=own_cohort, status=Assignment.Status.DRAFT)
        assignment_id, foreign_id = assignment.id, foreign_cohort.id

    resp = as_user(tenant_a, teacher_user).patch(
        f"/api/v1/assignments/{assignment_id}/", {"cohort": foreign_id}, format="json"
    )
    assert resp.status_code == 400
    with schema_context(tenant_a.schema_name):
        assignment.refresh_from_db()
        assert assignment.cohort_id == own_cohort.id  # unchanged


# --------------------------------------------------------------------------- #
# cross-tenant + query budgets
# --------------------------------------------------------------------------- #


def test_assignments_cross_tenant_isolated(tenant_a, tenant_b, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        AssignmentFactory(cohort=cohort, status=Assignment.Status.PUBLISHED)

    director_b = user_in(tenant_b, roles=["director"])
    body = as_user(tenant_b, director_b).get("/api/v1/assignments/").json()
    assert body["pagination"]["total"] == 0


# D2-D review: the Lane-D cross-tenant suite covered only the /assignments/ list.
# Pin tenant_mismatch (401) on the submission detail + action endpoints too — a
# tenant-A token on tenant-B's host must 401 at the TD-1 auth gate (before any
# object lookup), so arbitrary path ids are fine. (GET and POST methods per the
# endpoint's declared verbs.)
@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "/api/v1/assignments/1/submissions/"),  # teacher submissions list
        ("post", "/api/v1/assignments/1/submissions/"),  # student submit
        ("post", "/api/v1/assignments/upload-url/"),  # presigned upload
        ("get", "/api/v1/assignments/submissions/1/"),  # submission detail
        ("post", "/api/v1/assignments/submissions/1/grade/"),  # grade
        ("post", "/api/v1/assignments/submissions/1/request-ai-feedback/"),  # AI feedback
    ],
)
def test_assignment_action_endpoints_cross_tenant_rejected(
    tenant_a, tenant_b, user_in, client_for, method, url
):
    """A token minted in tenant A must 401 `tenant_mismatch` against tenant B's
    host on every submission/grade/upload action — not leak a 403/404/422."""
    from apps.auth.services import issue_token

    user = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        access = issue_token(user)["access"]

    client_b = client_for(tenant_b)
    client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = getattr(client_b, method)(url, {}, format="json")

    assert resp.status_code == 401
    assert resp.json()["code"] == "authentication_failed"


def test_assignments_list_query_budget(tenant_a, user_in, as_user, django_assert_max_num_queries):
    director = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        for _ in range(5):
            AssignmentFactory(cohort=cohort, status=Assignment.Status.PUBLISHED)

    client = as_user(tenant_a, director)
    with django_assert_max_num_queries(9):  # +1: A-2 per-request permission-override load
        body = client.get("/api/v1/assignments/").json()
    assert set(body) == {"success", "data", "pagination"}
    assert body["pagination"]["total"] == 5


def test_submissions_list_query_budget(tenant_a, user_in, as_user, django_assert_max_num_queries):
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        cohort = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        assignment: Any = AssignmentFactory(cohort=cohort, status=Assignment.Status.PUBLISHED)
        for _ in range(5):
            student = _member(cohort, branch)
            services.submit(assignment=assignment, student=student)
        assignment_id = assignment.id

    client = as_user(tenant_a, teacher_user)
    with django_assert_max_num_queries(9):  # +1: A-2 per-request permission-override load
        resp = client.get(f"/api/v1/assignments/{assignment_id}/submissions/")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 5


def test_scoped_assignments_helper(tenant_a, user_in):
    """Direct selector check — teacher sees own cohort incl. drafts."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher_profile = TeacherProfileFactory(user=teacher_user, branch=branch)
        cohort = CohortFactory(branch=branch, primary_teacher=teacher_profile)
        AssignmentFactory(cohort=cohort, status=Assignment.Status.DRAFT)
        AssignmentFactory(cohort=cohort, status=Assignment.Status.PUBLISHED)
        # An unrelated cohort's assignment must not appear.
        AssignmentFactory(cohort=CohortFactory(branch=branch), status=Assignment.Status.PUBLISHED)
        qs = selectors.scoped_assignments(user=teacher_user, roles={"teacher"})
        assert qs.count() == 2
