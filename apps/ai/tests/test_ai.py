"""AI integration tests: signals, tasks, endpoints, perms, isolation (D4-LA-6/7/8)."""

from __future__ import annotations

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.ai.models import AIRequest, TenantAIBudget
from apps.ai.tests.factories import AIPromptFactory, AIRequestFactory, make_budget
from apps.assignments.tests.factories import AssignmentFactory, SubmissionFactory
from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory

# ANTHROPIC_USE_MOCK defaults True in test settings (TD-2) — no override needed.
pytestmark = pytest.mark.django_db


def _seed_ai(tenant, *, daily=100_000, monthly=1_000_000, enabled=True):
    with schema_context(tenant.schema_name):
        AIPromptFactory(feature="assignment_feedback")
        AIPromptFactory(feature="exam_generation", token_cost_cap=12000)
        AIPromptFactory(feature="content_summary", token_cost_cap=3000)
        make_budget(daily_token_limit=daily, monthly_token_limit=monthly, is_enabled=enabled)


# ---------------------------------------------------------------------------
# Tasks (run synchronously via CELERY_TASK_ALWAYS_EAGER in test settings)
# ---------------------------------------------------------------------------


def test_assignment_feedback_task_succeeds_and_records(tenant_a):
    _seed_ai(tenant_a)
    from celery_tasks.ai_tasks import run_assignment_feedback

    with schema_context(tenant_a.schema_name):
        submission = SubmissionFactory(text="My essay about photosynthesis.")
        run_assignment_feedback(submission.pk)
        req = AIRequest.objects.get(feature="assignment_feedback", source_id=submission.pk)
        assert req.status == AIRequest.Status.SUCCEEDED
        assert req.output_text
        assert req.input_tokens > 0
        budget = TenantAIBudget.objects.get(pk=1)
        assert budget.tokens_used_today > 0
        submission.refresh_from_db()
        assert submission.grade.ai_feedback


def test_task_idempotent_on_redelivery(tenant_a):
    _seed_ai(tenant_a)
    from celery_tasks.ai_tasks import run_assignment_feedback

    with schema_context(tenant_a.schema_name):
        submission = SubmissionFactory()
        run_assignment_feedback(submission.pk)
        run_assignment_feedback(submission.pk)  # duplicate delivery
        assert AIRequest.objects.filter(source_id=submission.pk).count() == 1


def test_budget_exhausted_no_request_executed(tenant_a):
    _seed_ai(tenant_a, daily=1)
    from celery_tasks.ai_tasks import run_assignment_feedback

    with schema_context(tenant_a.schema_name):
        submission = SubmissionFactory()
        run_assignment_feedback(submission.pk)
        req = AIRequest.objects.get(source_id=submission.pk)
        assert req.status == AIRequest.Status.DENIED_BUDGET
        assert not req.output_text
        budget = TenantAIBudget.objects.get(pk=1)
        assert budget.tokens_used_today == 0


def test_denied_budget_request_is_redriven_after_budget_restored(tenant_a):
    """A budget denial is transient: once budget is restored, a re-request re-drives
    the SAME row to queued instead of returning the stale denied row forever."""
    from apps.ai.models import AIFeature
    from apps.ai.services import AIBudgetExceeded, check_and_reserve_budget

    _seed_ai(tenant_a, daily=1)  # tiny budget -> denial
    with schema_context(tenant_a.schema_name):
        with pytest.raises(AIBudgetExceeded):
            check_and_reserve_budget(
                feature=AIFeature.EXAM_GENERATION,
                estimated_tokens=5000,
                source_app="academics",
                source_id=77,
            )
        denied = AIRequest.objects.get(source_app="academics", source_id=77)
        assert denied.status == AIRequest.Status.DENIED_BUDGET
        make_budget(daily_token_limit=1_000_000, monthly_token_limit=10_000_000, is_enabled=True)
        req = check_and_reserve_budget(
            feature=AIFeature.EXAM_GENERATION,
            estimated_tokens=5000,
            source_app="academics",
            source_id=77,
        )
        assert req.pk == denied.pk  # same row re-driven, not a duplicate
        assert req.status == AIRequest.Status.QUEUED
        assert req.reserved_tokens == 5000
        assert TenantAIBudget.objects.get(pk=1).tokens_used_today == 5000  # reserved on re-drive


def test_succeeded_request_is_not_redriven(tenant_a):
    """A completed request is returned idempotently, never reset by a re-request."""
    from apps.ai.models import AIFeature
    from apps.ai.services import check_and_reserve_budget

    _seed_ai(tenant_a, daily=1_000_000)
    with schema_context(tenant_a.schema_name):
        first = check_and_reserve_budget(
            feature=AIFeature.EXAM_GENERATION,
            estimated_tokens=5000,
            source_app="academics",
            source_id=88,
        )
        AIRequest.objects.filter(pk=first.pk).update(status=AIRequest.Status.SUCCEEDED)
        again = check_and_reserve_budget(
            feature=AIFeature.EXAM_GENERATION,
            estimated_tokens=5000,
            source_app="academics",
            source_id=88,
        )
        assert again.pk == first.pk
        assert again.status == AIRequest.Status.SUCCEEDED  # idempotent, not reset


def test_exam_generation_idempotency_keys_on_params(tenant_a):
    """R3-P3: an AI run whose identity includes generation params must key on them — the
    SAME subject with DIFFERENT exam shape produces a NEW request (not the stale first),
    while identical params stay idempotent (a genuine retry/double-click coalesces)."""
    from apps.ai.models import AIFeature
    from apps.ai.services import check_and_reserve_budget

    _seed_ai(tenant_a, daily=1_000_000)
    with schema_context(tenant_a.schema_name):

        def _request(**params):
            return check_and_reserve_budget(
                feature=AIFeature.EXAM_GENERATION,
                estimated_tokens=5000,
                source_app="academics",
                source_id=42,
                params=params,
            )

        easy = _request(exam_type="quiz", question_count=10, difficulty="easy")
        hard = _request(exam_type="quiz", question_count=50, difficulty="hard")
        assert hard.pk != easy.pk  # different params -> a distinct generation, not stale reuse

        again = _request(exam_type="quiz", question_count=10, difficulty="easy")
        assert again.pk == easy.pk  # identical params -> idempotent


def test_duplicate_queued_exam_request_is_not_enqueued_twice(
    tenant_a, django_capture_on_commit_callbacks, monkeypatch
):
    """A retry/double-click that finds the existing QUEUED row must not publish
    another Celery message for the same idempotency key."""
    from django.core.cache import cache

    from apps.ai import services
    from apps.org.models import CenterSettings

    _seed_ai(tenant_a, daily=1_000_000)
    enqueued: list[int] = []
    monkeypatch.setattr(
        services,
        "_enqueue_exam_generation",
        lambda request_id, params, schema: enqueued.append(request_id),
    )

    with schema_context(tenant_a.schema_name):
        center_settings = CenterSettings.load()
        center_settings.ai_exam_generation_enabled = True
        center_settings.save(update_fields=["ai_exam_generation_enabled"])
        cache.clear()
        params = {
            "subject_id": 42,
            "exam_type": "quiz",
            "question_count": 10,
            "difficulty": "easy",
        }
        with django_capture_on_commit_callbacks(execute=True):
            first = services.request_exam_generation(**params)
        with django_capture_on_commit_callbacks(execute=True):
            again = services.request_exam_generation(**params)

        assert again.pk == first.pk
        assert enqueued == [first.pk]


def test_redaction_applied_before_complete(tenant_a, monkeypatch):
    _seed_ai(tenant_a)
    from celery_tasks import ai_tasks

    captured = {}

    def _fake_complete(*, messages, system, max_tokens, effort):
        captured["text"] = messages[0]["content"]
        return {
            "text": "Feedback for [STUDENT_1]: good work.",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    monkeypatch.setattr(ai_tasks, "complete", _fake_complete)

    with schema_context(tenant_a.schema_name):
        from apps.students.tests.factories import StudentProfileFactory

        student = StudentProfileFactory(user__first_name="Ali", user__last_name="Valiyev")
        submission = SubmissionFactory(student=student, text="Reach me at +998901234567 or ali@example.com")
        ai_tasks.run_assignment_feedback(submission.pk)
        # The prompt sent to complete() must NOT contain the raw PII.
        assert "+998901234567" not in captured["text"]
        assert "ali@example.com" not in captured["text"]
        # The stored output restored the [STUDENT_1] token back to the real name.
        req = AIRequest.objects.get(source_id=submission.pk)
        assert "Ali Valiyev" in req.output_text


def test_content_summary_task(tenant_a, monkeypatch):
    _seed_ai(tenant_a)
    from celery_tasks.ai_tasks import run_content_summary

    with schema_context(tenant_a.schema_name):
        from apps.content.models import ContentLibrary, Folder, LessonFile

        lib = ContentLibrary.objects.create(name="Lib")
        folder = Folder.objects.create(library=lib, name="F")
        lf = LessonFile.objects.create(
            folder=folder,
            title="Notes",
            s3_key="tenant_a/content/1/notes.pdf",
            content_type="application/pdf",
            size_bytes=1000,
            status=LessonFile.Status.CLEAN,
        )
        run_content_summary(lf.pk)
        req = AIRequest.objects.get(feature="content_summary", source_id=lf.pk)
        assert req.status == AIRequest.Status.SUCCEEDED


# ---------------------------------------------------------------------------
# Retry / reliability (review fix: a transient failure must actually re-execute)
# ---------------------------------------------------------------------------
class _FakeTask:
    """Minimal Celery-task stand-in to drive _run_with_retry's exhaustion branch
    deterministically (eager Celery never increments request.retries, so the real
    task can't reach exhaustion in-process)."""

    def __init__(self, *, retries: int, max_retries: int = 3):
        self.max_retries = max_retries
        self.request = type("R", (), {"retries": retries})()

    def retry(self, exc=None):  # pragma: no cover - not reached on the exhaustion path
        from celery.exceptions import Retry

        raise Retry(exc=exc)


def test_transient_failure_leaves_running_then_retry_reexecutes(tenant_a, monkeypatch):
    """A transient error must NOT terminally fail the request: it stays RUNNING so
    the redelivered (retried) task re-executes instead of short-circuiting. Models
    the real flow — eager self.retry() raises Retry; Celery then re-delivers, which
    we simulate by re-invoking the task."""
    from celery.exceptions import Retry

    _seed_ai(tenant_a)
    from celery_tasks import ai_tasks

    calls = {"n": 0}

    def _flaky_complete(*, messages, system, max_tokens, effort):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient 529 overload")
        return {"text": "feedback [STUDENT_1]", "usage": {"input_tokens": 10, "output_tokens": 5}}

    monkeypatch.setattr(ai_tasks, "complete", _flaky_complete)

    with schema_context(tenant_a.schema_name):
        submission = SubmissionFactory(text="essay")
        # 1st delivery: transient failure → eager retry raises Retry. The request
        # must be left RUNNING (the fix), NOT terminal FAILED.
        with pytest.raises(Retry):
            ai_tasks.run_assignment_feedback.apply(args=[submission.pk])
        req = AIRequest.objects.get(source_id=submission.pk)
        assert req.status == AIRequest.Status.RUNNING  # would be FAILED before the fix
        # 2nd delivery (the retry): must RE-EXECUTE (task-body + _run_request guards
        # both let RUNNING through) and succeed — not no-op.
        ai_tasks.run_assignment_feedback.apply(args=[submission.pk])
        req.refresh_from_db()
        assert req.status == AIRequest.Status.SUCCEEDED
        assert calls["n"] == 2


def test_exhausted_retries_mark_failed_and_release_reservation(tenant_a, monkeypatch):
    """When retries are exhausted the request is terminally FAILED and its reserved
    budget tokens are released (not silently consumed)."""
    _seed_ai(tenant_a)
    from apps.ai.models import AIFeature
    from apps.ai.services import check_and_reserve_budget
    from celery_tasks import ai_tasks

    def _always_fails(*, messages, system, max_tokens, effort):
        raise RuntimeError("permanent")

    monkeypatch.setattr(ai_tasks, "complete", _always_fails)

    with schema_context(tenant_a.schema_name):
        submission = SubmissionFactory(text="essay")
        req = check_and_reserve_budget(
            feature=AIFeature.ASSIGNMENT_FEEDBACK,
            estimated_tokens=4000,
            source_app="assignments",
            source_id=submission.pk,
        )
        assert TenantAIBudget.objects.get(pk=1).tokens_used_today == 4000  # reserved
        task = _FakeTask(retries=3, max_retries=3)  # retries exhausted

        def _build(prompt, request):
            return "body", [], (lambda restored: None)

        with pytest.raises(RuntimeError):
            ai_tasks._run_with_retry(task, req.pk, build_prompt=_build)
        req.refresh_from_db()
        assert req.status == AIRequest.Status.FAILED
        assert req.reserved_tokens == 0
        # Reservation released → budget back to baseline.
        assert TenantAIBudget.objects.get(pk=1).tokens_used_today == 0


# ---------------------------------------------------------------------------
# Budget reservation (review fix: reserve at queue time so bursts can't overspend)
# ---------------------------------------------------------------------------
def test_reservation_blocks_burst_before_completion(tenant_a):
    """check_and_reserve_budget must RESERVE (increment) at queue time, so a second
    in-flight request sees the first's reservation and is denied — not both passing
    a stale check and collectively exceeding the cap."""
    _seed_ai(tenant_a, daily=5000)  # one 4000-token estimate fits; two do not
    from apps.ai.models import AIFeature
    from apps.ai.services import AIBudgetExceeded, check_and_reserve_budget

    with schema_context(tenant_a.schema_name):
        first = check_and_reserve_budget(
            feature=AIFeature.ASSIGNMENT_FEEDBACK,
            estimated_tokens=4000,
            source_app="assignments",
            source_id=101,
        )
        assert first.status == AIRequest.Status.QUEUED
        assert first.reserved_tokens == 4000
        # Reserved immediately — before any record_usage / completion.
        assert TenantAIBudget.objects.get(pk=1).tokens_used_today == 4000
        with pytest.raises(AIBudgetExceeded):
            check_and_reserve_budget(
                feature=AIFeature.ASSIGNMENT_FEEDBACK,
                estimated_tokens=4000,
                source_app="assignments",
                source_id=102,
            )


def test_cache_hit_bills_zero_tokens(tenant_a):
    """A Redis response-cache hit purchased nothing, so it must bill zero extra
    budget (the reserved estimate is released on reconcile)."""
    _seed_ai(tenant_a)
    from celery_tasks.ai_tasks import run_assignment_feedback

    with schema_context(tenant_a.schema_name):
        assignment = AssignmentFactory()
        # Identical assignment + text + (redacted) student token => identical
        # redacted prompt => the 2nd run is a response-cache hit.
        s1 = SubmissionFactory(assignment=assignment, text="same body for caching")
        s2 = SubmissionFactory(assignment=assignment, text="same body for caching")
        run_assignment_feedback(s1.pk)
        first = TenantAIBudget.objects.get(pk=1).tokens_used_today
        assert first > 0
        run_assignment_feedback(s2.pk)
        second = TenantAIBudget.objects.get(pk=1).tokens_used_today
        assert second == first  # cache hit added nothing


def test_third_party_pii_redacted_before_complete(tenant_a, monkeypatch):
    """Free-text submissions naming a guardian / carrying a plain (non-+) phone
    must be redacted before the prompt leaves for the model."""
    _seed_ai(tenant_a)
    from celery_tasks import ai_tasks

    captured = {}

    def _fake_complete(*, messages, system, max_tokens, effort):
        captured["text"] = messages[0]["content"]
        return {"text": "ok", "usage": {"input_tokens": 10, "output_tokens": 5}}

    monkeypatch.setattr(ai_tasks, "complete", _fake_complete)

    with schema_context(tenant_a.schema_name):
        from apps.parents.tests.factories import GuardianFactory
        from apps.students.tests.factories import StudentProfileFactory

        student = StudentProfileFactory(user__first_name="Ali", user__last_name="Valiyev")
        GuardianFactory(
            student=student,
            parent__user__first_name="Dilnoza",
            parent__user__last_name="Karimova",
        )
        submission = SubmissionFactory(
            student=student,
            text="My mother Dilnoza Karimova can be reached at 90 123 45 67.",
        )
        ai_tasks.run_assignment_feedback(submission.pk)
        assert "Dilnoza Karimova" not in captured["text"]  # guardian name tokenized
        assert "90 123 45 67" not in captured["text"]  # plain phone tokenized


# ---------------------------------------------------------------------------
# Signal wiring (D4-LA-7)
# ---------------------------------------------------------------------------


def test_submission_enqueues_feedback_once(tenant_a, django_capture_on_commit_callbacks):
    _seed_ai(tenant_a)
    with schema_context(tenant_a.schema_name):
        from apps.assignments.services import submit

        cohort = CohortFactory()
        assignment = AssignmentFactory(cohort=cohort)
        from apps.students.tests.factories import StudentProfileFactory

        student = StudentProfileFactory()
        CohortMembershipFactory(cohort=cohort, student=student)
        with django_capture_on_commit_callbacks(execute=True):
            submit(assignment=assignment, student=student, text="done")
        rows = AIRequest.objects.filter(feature="assignment_feedback")
        assert rows.count() == 1


def test_file_confirm_enqueues_summary(tenant_a, django_capture_on_commit_callbacks, monkeypatch):
    _seed_ai(tenant_a)
    # confirm_upload also enqueues the content validate task (S3); stub it out so
    # this test isolates the AI-summary signal wiring.
    from celery_tasks import content_tasks

    monkeypatch.setattr(content_tasks.validate_uploaded_file, "delay", lambda *a, **k: None)

    with schema_context(tenant_a.schema_name):
        from apps.content.models import ContentLibrary, Folder, LessonFile
        from apps.content.services import confirm_upload

        lib = ContentLibrary.objects.create(name="Lib")
        folder = Folder.objects.create(library=lib, name="F")
        lf = LessonFile.objects.create(
            folder=folder,
            title="Notes",
            s3_key="tenant_a/tmp/x/notes.pdf",
            content_type="application/pdf",
            size_bytes=1000,
            status=LessonFile.Status.PENDING,
        )
        with django_capture_on_commit_callbacks(execute=True):
            confirm_upload(file=lf)
        assert AIRequest.objects.filter(feature="content_summary", source_id=lf.pk).exists()


# ---------------------------------------------------------------------------
# Endpoints (D4-LA-8)
# ---------------------------------------------------------------------------


def test_requests_log_lists_for_teacher(tenant_a, as_role):
    _seed_ai(tenant_a)
    with schema_context(tenant_a.schema_name):
        AIRequestFactory.create_batch(3)
    client, _ = as_role("teacher", tenant_a)
    resp = client.get("/api/v1/ai/requests/")
    assert resp.status_code == 200
    assert resp.json()["pagination"]["total"] == 3


def test_requests_list_never_exposes_output_text(tenant_a, as_role):
    _seed_ai(tenant_a)
    client, user = as_role("teacher", tenant_a)
    with schema_context(tenant_a.schema_name):
        AIRequestFactory(requested_by=user, output_text="private generated feedback")

    resp = client.get("/api/v1/ai/requests/")

    assert resp.status_code == 200
    assert "output_text" not in resp.json()["data"][0]


def test_requester_can_read_own_ai_output(tenant_a, as_role):
    _seed_ai(tenant_a)
    client, user = as_role("teacher", tenant_a)
    with schema_context(tenant_a.schema_name):
        ai_request = AIRequestFactory(requested_by=user, output_text="private generated feedback")

    resp = client.get(f"/api/v1/ai/requests/{ai_request.pk}/")

    assert resp.status_code == 200
    assert resp.json()["data"]["output_text"] == "private generated feedback"


def test_other_reader_cannot_read_ai_output(tenant_a, as_role):
    _seed_ai(tenant_a)
    _, requester = as_role("teacher", tenant_a)
    reader, _ = as_role("teacher", tenant_a)
    with schema_context(tenant_a.schema_name):
        ai_request = AIRequestFactory(
            requested_by=requester,
            output_text="private generated feedback",
        )

    resp = reader.get(f"/api/v1/ai/requests/{ai_request.pk}/")

    assert resp.status_code == 200
    assert "output_text" not in resp.json()["data"]


def test_requests_log_cross_tenant_isolation(tenant_a, tenant_b, as_role):
    _seed_ai(tenant_a)
    with schema_context(tenant_a.schema_name):
        AIRequestFactory.create_batch(2)
    client, _ = as_role("teacher", tenant_b)  # tenant B token
    resp = client.get("/api/v1/ai/requests/")
    assert resp.status_code == 200
    assert resp.json()["pagination"]["total"] == 0


@pytest.mark.parametrize("role", ["student", "parent"])
def test_endpoints_forbidden_for_student_parent(tenant_a, as_role, role):
    _seed_ai(tenant_a)
    client, _ = as_role(role, tenant_a)
    assert client.get("/api/v1/ai/requests/").status_code == 403
    assert client.get("/api/v1/ai/budget/").status_code == 403
    assert client.get("/api/v1/ai/usage-report/").status_code == 403
    assert (
        client.post(
            "/api/v1/ai/exam-generation/",
            {"subject_id": 1, "exam_type": "quiz", "question_count": 5, "difficulty": "easy"},
            format="json",
        ).status_code
        == 403
    )


def test_budget_get_and_patch(tenant_a, as_role):
    _seed_ai(tenant_a)
    teacher, _ = as_role("teacher", tenant_a)
    assert teacher.get("/api/v1/ai/budget/").status_code == 200
    # Teacher cannot PATCH (ai:manage is director-only).
    assert teacher.patch("/api/v1/ai/budget/", {"is_enabled": False}, format="json").status_code == 403

    director, _ = as_role("director", tenant_a)
    resp = director.patch("/api/v1/ai/budget/", {"daily_token_limit": 555}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["daily_token_limit"] == 555


def test_budget_patch_bool_parity(tenant_a, as_role):
    """is_enabled parses DRF-BooleanField-compatibly: "on"/"y" -> True, and a garbage
    string is a 400 (NOT a silent coerce to False that would disable AI center-wide)."""
    _seed_ai(tenant_a)
    director, _ = as_role("director", tenant_a)
    on = director.patch("/api/v1/ai/budget/", {"is_enabled": "on"}, format="json")
    assert on.status_code == 200, on.content
    assert on.json()["data"]["is_enabled"] is True
    garbage = director.patch("/api/v1/ai/budget/", {"is_enabled": "maybe"}, format="json")
    assert garbage.status_code == 400  # not a silent False


def test_usage_report_month_year_boundary_is_400_not_500(tenant_a, as_role):
    """?month=9999-12 parses but its next-month rollover to year 10000 raises ValueError;
    it must be a clean 400 invalid_month, not a 500."""
    _seed_ai(tenant_a)
    teacher, _ = as_role("teacher", tenant_a)
    resp = teacher.get("/api/v1/ai/usage-report/?month=9999-12")
    assert resp.status_code == 400, resp.content


def test_requests_log_created_after_accepts_date_only(tenant_a, as_role):
    """created_after/created_before accept a date-only value (parity with the old
    DateTimeFilter's DATETIME_INPUT_FORMATS) instead of 400-ing on it."""
    _seed_ai(tenant_a)
    teacher, _ = as_role("teacher", tenant_a)
    resp = teacher.get("/api/v1/ai/requests/?created_after=2020-01-01")
    assert resp.status_code == 200, resp.content


def test_exam_generation_gated_by_center_settings(tenant_a, as_role):
    _seed_ai(tenant_a)
    teacher, _ = as_role("teacher", tenant_a)
    body = {"subject_id": 1, "exam_type": "quiz", "question_count": 5, "difficulty": "easy"}
    # Gate off by default -> 403 feature_disabled.
    resp = teacher.post("/api/v1/ai/exam-generation/", body, format="json")
    assert resp.status_code == 403
    assert resp.json()["code"] == "feature_disabled"

    # Flip the knob on -> 202 with a request id.
    with schema_context(tenant_a.schema_name):
        from apps.org.models import CenterSettings

        cs = CenterSettings.load()
        cs.ai_exam_generation_enabled = True
        cs.save()
    from django.core.cache import cache

    cache.clear()
    resp = teacher.post("/api/v1/ai/exam-generation/", body, format="json")
    assert resp.status_code == 202
    assert "request_id" in resp.json()["data"]


def test_exam_generation_over_budget_429(tenant_a, as_role):
    _seed_ai(tenant_a, daily=1)
    with schema_context(tenant_a.schema_name):
        from apps.org.models import CenterSettings

        cs = CenterSettings.load()
        cs.ai_exam_generation_enabled = True
        cs.save()
    from django.core.cache import cache

    cache.clear()
    teacher, _ = as_role("teacher", tenant_a)
    body = {"subject_id": 1, "exam_type": "quiz", "question_count": 5, "difficulty": "easy"}
    resp = teacher.post("/api/v1/ai/exam-generation/", body, format="json")
    assert resp.status_code == 429
    assert resp.json()["code"] == "ai_budget_exceeded"


def test_usage_report(tenant_a, as_role):
    _seed_ai(tenant_a)
    with schema_context(tenant_a.schema_name):
        AIRequestFactory(feature="assignment_feedback", input_tokens=100, output_tokens=50)
        AIRequestFactory(
            feature="assignment_feedback",
            input_tokens=200,
            output_tokens=80,
            idempotency_key="assignment_feedback:assignments:999:v1",
            source_id=999,
        )
    teacher, _ = as_role("teacher", tenant_a)
    month = timezone.localdate().strftime("%Y-%m")
    resp = teacher.get(f"/api/v1/ai/usage-report/?month={month}")
    assert resp.status_code == 200
    row = next(r for r in resp.json()["data"] if r["feature"] == "assignment_feedback")
    assert row["requests"] == 2
    assert row["input_tokens"] == 300
    assert row["output_tokens"] == 130


def test_requests_list_query_count(tenant_a, as_role, django_assert_max_num_queries):
    _seed_ai(tenant_a)
    with schema_context(tenant_a.schema_name):
        AIRequestFactory.create_batch(5)
    client, _ = as_role("teacher", tenant_a)
    with django_assert_max_num_queries(10):
        resp = client.get("/api/v1/ai/requests/")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Selector (D4-LA-9) + billing compatibility
# ---------------------------------------------------------------------------


def test_tokens_consumed_sums_window(tenant_a):
    with schema_context(tenant_a.schema_name):
        AIPromptFactory()
        AIRequestFactory(input_tokens=100, output_tokens=50)
        AIRequestFactory(input_tokens=200, output_tokens=30, source_id=2, idempotency_key="k2")
        AIRequestFactory(input_tokens=10, output_tokens=10, source_id=3, idempotency_key="k3")
        from apps.ai.selectors import tokens_consumed, tokens_used_current_month

        today = timezone.localdate()
        assert tokens_consumed(today, today) == 100 + 50 + 200 + 30 + 10 + 10
        # Billing's lazily-imported function still works (delegates to tokens_consumed).
        assert tokens_used_current_month() == 400


def test_celery_task_runs_under_scheduling_schema(tenant_a, tenant_b):
    """A task enqueued with _schema_name activates the right schema (TASKS §26)."""
    _seed_ai(tenant_a)
    from celery_tasks.ai_tasks import run_assignment_feedback

    with schema_context(tenant_a.schema_name):
        submission = SubmissionFactory()
    # Enqueue from public context, pointing at tenant_a's schema.
    run_assignment_feedback.delay(submission.pk, _schema_name=tenant_a.schema_name)
    with schema_context(tenant_a.schema_name):
        assert AIRequest.objects.filter(source_id=submission.pk).exists()
    with schema_context(tenant_b.schema_name):
        assert not AIRequest.objects.filter(source_id=submission.pk).exists()
