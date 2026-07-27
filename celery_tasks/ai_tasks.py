"""AI feature tasks (D4-LA-6). Celery-only, schema-scoped, idempotent.

Each task: load the active ``AIPrompt`` for its feature → build the user prompt
from source data → redact PII (E.164 / national-id / email / known names) →
``infrastructure.ai.anthropic_client.complete`` (mock-first, TD-2) → restore the
tokens in the model output → persist the output on the source row + the
``AIRequest`` → ``record_usage`` to reconcile the budget.

Idempotency: every task is anchored to an ``AIRequest`` resolved by its
idempotency key, and short-circuits unless the request is still
``queued``/``running`` — a Celery retry or a duplicate delivery never re-bills or
double-writes. Transient failures retry (max_retries=3, exponential backoff,
``acks_late``) leaving the request ``running`` so the retry actually re-executes;
only once retries are exhausted is it set ``status=failed`` + ``error_detail`` and
its budget reservation released.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from config.celery import app

# Module-level so tests can monkeypatch `ai_tasks.complete`. The anthropic client
# imports only settings + core.utils (no Django models), so this is import-safe.
from infrastructure.ai.anthropic_client import complete

logger = logging.getLogger("starforge.ai")


# ---------------------------------------------------------------------------
# Shared execution helper
# ---------------------------------------------------------------------------


def _run_request(ai_request_id: int, *, build_prompt) -> str:
    """Execute one ``AIRequest`` end to end.

    ``build_prompt(prompt, request)`` returns ``(user_text, known_names,
    persist)`` where ``persist(restored_text)`` writes the feature-specific
    output to its source row. Returns the final ``AIRequest.status``.
    """
    from apps.ai.models import AIRequest
    from apps.ai.redaction import dump_map, redact, restore
    from apps.ai.services import Usage, record_usage

    request = AIRequest.objects.select_related("prompt").get(pk=ai_request_id)
    if request.status not in (AIRequest.Status.QUEUED, AIRequest.Status.RUNNING):
        # Already terminal (succeeded / failed / denied) — idempotent no-op.
        return request.status

    request.status = AIRequest.Status.RUNNING
    request.started_at = request.started_at or timezone.now()
    request.save(update_fields=["status", "started_at"])

    prompt = request.prompt
    user_text, known_names, persist = build_prompt(prompt, request)

    redacted_text, mapping = redact(user_text, known_names=known_names)
    request.redaction_map = dump_map(mapping)
    request.save(update_fields=["redaction_map"])

    result = complete(
        system=prompt.system_prompt,
        messages=[{"role": "user", "content": redacted_text}],
        max_tokens=prompt.max_output_tokens,
        effort=prompt.effort,
    )
    restored = restore(result.get("text", ""), mapping)

    persist(restored)

    # Reconcile usage onto the budget WHILE the request is still RUNNING (the
    # record_usage guard requires queued/running), then mark it succeeded. A Redis
    # response-cache hit purchased nothing, so it is billed at zero (the reserved
    # estimate is released) — see anthropic_client cache_hit flag.
    record_usage(
        ai_request_id=request.pk,
        usage=Usage.from_dict(result.get("usage", {})),
        billable=not result.get("cache_hit", False),
    )

    request.output_text = restored
    request.status = AIRequest.Status.SUCCEEDED
    request.finished_at = timezone.now()
    request.save(update_fields=["output_text", "status", "finished_at"])
    return request.status


def _mark_failed(ai_request_id: int, exc: Exception) -> None:
    """Mark a request terminally FAILED and release its budget reservation.

    Only called once retries are exhausted (see ``_run_with_retry``). Skips a row
    that already reached a terminal SUCCEEDED/DENIED state so a late failure can't
    clobber a success."""
    from apps.ai.models import AIRequest
    from apps.ai.services import release_reservation

    try:
        request = AIRequest.objects.get(pk=ai_request_id)
    except AIRequest.DoesNotExist:
        return
    if request.status in (AIRequest.Status.SUCCEEDED, AIRequest.Status.DENIED_BUDGET):
        return
    from apps.ai.redaction import redact

    release_reservation(ai_request_id=ai_request_id)
    request.status = AIRequest.Status.FAILED
    # Scrub PII (phone/email/national-id) the exception may have echoed from the
    # prompt before persisting it to this plaintext column.
    request.error_detail = redact(f"{type(exc).__name__}: {exc}")[0][:2000]
    request.finished_at = timezone.now()
    request.save(update_fields=["status", "error_detail", "finished_at"])


def _run_with_retry(task, ai_request_id: int, *, build_prompt) -> str | None:
    """Run a request, retrying transient failures with backoff.

    CRITICAL: on an intermediate attempt we do NOT mark the request FAILED — a
    terminal status would make ``_run_request`` short-circuit on the retry (its
    guard only proceeds for queued/running), so the retry would be a silent no-op
    and the whole retry/backoff feature would be dead. We leave the row RUNNING
    and only mark it FAILED (releasing the reservation) once retries are
    exhausted."""
    try:
        return _run_request(ai_request_id, build_prompt=build_prompt)
    except Exception as exc:
        if task.request.retries >= task.max_retries:
            _mark_failed(ai_request_id, exc)
            raise
        raise task.retry(exc=exc) from exc


# ---------------------------------------------------------------------------
# Assignment feedback
# ---------------------------------------------------------------------------


@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def run_assignment_feedback(self, submission_id: int, *, requested_by: int | None = None) -> str | None:
    """Generate AI feedback for one submission and store it on its
    ``SubmissionGrade.ai_feedback`` (the reserved Day-2 field)."""
    from apps.ai.models import AIFeature
    from apps.ai.services import AIBudgetExceeded, check_and_reserve_budget
    from apps.assignments.models import Submission, SubmissionGrade

    try:
        submission = Submission.objects.select_related("assignment", "student__user").get(pk=submission_id)
    except Submission.DoesNotExist:
        logger.warning("run_assignment_feedback: submission %s gone", submission_id)
        return None

    try:
        ai_request = check_and_reserve_budget(
            feature=AIFeature.ASSIGNMENT_FEEDBACK,
            estimated_tokens=_prompt_cap(AIFeature.ASSIGNMENT_FEEDBACK),
            requested_by_id=requested_by,
            source_app="assignments",
            source_id=submission_id,
        )
    except AIBudgetExceeded:
        # Over budget: the denied AIRequest row is recorded by the service; do
        # not enqueue/execute. This is the "budget exhausted -> nothing runs" path.
        logger.info("run_assignment_feedback: budget exceeded for submission %s", submission_id)
        return None

    if ai_request.status not in (ai_request.Status.QUEUED, ai_request.Status.RUNNING):
        return ai_request.status  # terminal (succeeded/failed/denied) — idempotent skip.
        # NB: RUNNING must fall through so a Celery retry re-executes (a transient
        # failure leaves the row RUNNING — see _run_with_retry).

    def _build(prompt, request):
        student_name = submission.student.user.get_full_name() or ""
        body = prompt.user_template.format(
            assignment_title=submission.assignment.title,
            submission_text=submission.text or "",
            student_name=student_name or "the student",
        )
        # Free-text submissions routinely name third parties (parents/guardians),
        # which a [student]-only redaction would leak. Tokenize the student AND
        # every linked guardian name; structured PII (phones/emails/ids) is caught
        # by the regexes in redaction.py.
        names = [student_name] if student_name else []
        guardian_names = (
            submission.student.guardians.select_related("parent__user")
            .all()
            .values_list("parent__user__first_name", "parent__user__last_name")
        )
        for first, last in guardian_names:
            full = f"{first or ''} {last or ''}".strip()
            if full:
                names.append(full)

        def _persist(restored: str) -> None:
            # Write AI feedback onto the (possibly not-yet-graded) SubmissionGrade
            # WITHOUT touching the teacher's score: update if a grade exists, else
            # create a placeholder row carrying only the AI feedback (score=0).
            updated = SubmissionGrade.objects.filter(submission=submission).update(ai_feedback=restored)
            if not updated:
                from decimal import Decimal

                SubmissionGrade.objects.create(
                    submission=submission, score=Decimal("0"), ai_feedback=restored
                )

        return body, names, _persist

    return _run_with_retry(self, ai_request.pk, build_prompt=_build)


# ---------------------------------------------------------------------------
# Exam generation (request-driven)
# ---------------------------------------------------------------------------


@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def run_exam_generation(self, ai_request_id: int, *, params: dict | None = None) -> str | None:
    """Generate exam questions for the requested subject; the output text is
    stored on the ``AIRequest`` (consumed by the academics exam-authoring UI)."""
    params = params or {}

    def _build(prompt, request):
        from apps.academics.models import Subject

        subject_id = int(params.get("subject_id") or 0)
        subject = Subject.objects.filter(pk=subject_id).first()
        subject_name = subject.name if subject is not None else "the subject"
        body = prompt.user_template.format(
            subject_name=subject_name,
            exam_type=params.get("exam_type", "quiz"),
            question_count=params.get("question_count", 10),
            difficulty=params.get("difficulty", "medium"),
        )
        # Exam prompts contain no student PII; no known names to redact.
        return body, [], lambda restored: None

    return _run_with_retry(self, ai_request_id, build_prompt=_build)


# ---------------------------------------------------------------------------
# Content summary
# ---------------------------------------------------------------------------


@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def run_content_summary(self, lesson_file_id: int, *, requested_by: int | None = None) -> str | None:
    """Summarize a confirmed content file; the summary is stored on the
    ``AIRequest`` output (a future content field can read it)."""
    from apps.ai.models import AIFeature
    from apps.ai.services import AIBudgetExceeded, check_and_reserve_budget
    from apps.content.models import LessonFile

    try:
        lesson_file = LessonFile.objects.get(pk=lesson_file_id)
    except LessonFile.DoesNotExist:
        logger.warning("run_content_summary: lesson file %s gone", lesson_file_id)
        return None

    try:
        ai_request = check_and_reserve_budget(
            feature=AIFeature.CONTENT_SUMMARY,
            estimated_tokens=_prompt_cap(AIFeature.CONTENT_SUMMARY),
            requested_by_id=requested_by,
            source_app="content",
            source_id=lesson_file_id,
        )
    except AIBudgetExceeded:
        logger.info("run_content_summary: budget exceeded for file %s", lesson_file_id)
        return None

    if ai_request.status not in (ai_request.Status.QUEUED, ai_request.Status.RUNNING):
        return ai_request.status  # terminal — idempotent skip; RUNNING retries re-execute.

    def _build(prompt, request):
        body = prompt.user_template.format(
            file_title=lesson_file.title,
            file_type=lesson_file.content_type,
        )
        return body, [], lambda restored: None

    return _run_with_retry(self, ai_request.pk, build_prompt=_build)


# ---------------------------------------------------------------------------
# Placement test generation (F1-3)
# ---------------------------------------------------------------------------


@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def run_placement_generation(self, ai_request_id: int, *, params: dict | None = None) -> str | None:
    """Generate placement questions for a draft test; the persist hook parses the
    JSON output and appends the valid questions to the DRAFT test."""
    params = params or {}

    test_id = int(params.get("test_id") or 0)

    def _build(prompt, request):
        from apps.placement.models import PlacementTest
        from apps.placement.services import apply_generated_questions

        test = PlacementTest.objects.filter(pk=test_id).select_related("subject").first()
        subject_name = test.subject.name if test is not None and test.subject is not None else "general"
        body = prompt.user_template.format(
            subject=subject_name,
            count=params.get("count", 10),
            difficulty=params.get("difficulty", "medium"),
            topic=params.get("topic") or "general placement",
        )
        # No student PII in a placement-gen prompt; persist applies the questions.
        return (
            body,
            [],
            lambda restored: apply_generated_questions(test_id=test_id, output_text=restored),
        )

    return _run_with_retry(self, ai_request_id, build_prompt=_build)


# ---------------------------------------------------------------------------
# Form response analysis (F3-4)
# ---------------------------------------------------------------------------

# Keep the free-text input within the reserved token budget (cap ~4000 tokens ≈
# 16k chars; leave headroom for the aggregate + system prompt).
_MAX_ANALYSIS_COMMENT_CHARS = 12_000


@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def run_form_analysis(self, ai_request_id: int, *, params: dict | None = None) -> str | None:
    """Analyze a form's responses (aggregate + free-text comments); the narrative is
    stored on the AIRequest output. Respondent names are redacted before sending."""
    params = params or {}
    form_id = int(params.get("form_id") or 0)

    def _build(prompt, request):
        from apps.forms.models import Form, FormField
        from apps.forms.services import form_summary

        form = Form.objects.filter(pk=form_id).first()
        if form is None:
            return "Form: (unavailable)\n", [], lambda restored: None
        summary = form_summary(form)
        agg_lines = [f"Responses: {summary['response_count']}"]
        for field in summary["fields"]:
            agg_lines.append(f"- {field['label']} ({field['field_type']}): {field['summary']}")
        comment_blocks = []
        for f in form.fields.filter(
            field_type__in=(FormField.FieldType.TEXT, FormField.FieldType.TEXTAREA)
        ):
            answers = [
                a for a in f.answers.values_list("value", flat=True) if isinstance(a, str) and a.strip()
            ]
            if answers:
                comment_blocks.append(f.label + ":\n" + "\n".join("  - " + a for a in answers))
        # Tokenize respondent names that may appear in free text; structured PII
        # (phones/emails/ids) is caught by the regexes in redaction.py.
        names = []
        for r in form.responses.select_related("respondent"):
            if r.respondent is not None:
                full = r.respondent.get_full_name()
                if full:
                    names.append(full)
        # Bound the free-text volume so a large form can't push the prompt past the
        # reserved token budget (or the model context window) — form analysis is the
        # only feature that aggregates many respondents' text into one call.
        comments = "\n\n".join(comment_blocks) or "(no free-text answers)"
        if len(comments) > _MAX_ANALYSIS_COMMENT_CHARS:
            comments = (
                comments[:_MAX_ANALYSIS_COMMENT_CHARS] + "\n…(truncated; analyze this representative sample)"
            )
        body = prompt.user_template.format(
            form_title=form.title, aggregate="\n".join(agg_lines), comments=comments
        )
        return body, names, lambda restored: None

    return _run_with_retry(self, ai_request_id, build_prompt=_build)


# ---------------------------------------------------------------------------
# Placement writing marking (F8-3)
# ---------------------------------------------------------------------------

_MAX_MARKING_CHARS = 12_000  # keep the writing answers within the reserved token budget


@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def run_writing_marking(self, ai_request_id: int, *, params: dict | None = None) -> str | None:
    """Score a placement attempt's writing answers; the persist hook clamps + applies
    the scores and recomputes the attempt grade."""
    params = params or {}
    attempt_id = int(params.get("attempt_id") or 0)

    def _build(prompt, request):
        from apps.placement.models import PlacementAttempt, PlacementQuestion
        from apps.placement.services import apply_writing_marks

        attempt = PlacementAttempt.objects.filter(pk=attempt_id).select_related("student").first()
        if attempt is None:
            return "No attempt.\n", [], lambda restored: None
        writing = attempt.answers.select_related("question").filter(
            question__question_type=PlacementQuestion.QuestionType.WRITING
        )
        item_lines = [
            f"question_id {a.question_id} (max {a.question.points} pts)\n"
            f"  prompt: {a.question.prompt}\n"
            f"  answer: {a.response}"
            for a in writing
        ]
        items = "\n\n".join(item_lines) or "(none)"
        if len(items) > _MAX_MARKING_CHARS:
            items = items[:_MAX_MARKING_CHARS] + "\n…(truncated)"
        body = prompt.user_template.format(items=items)
        # The lead wrote these answers; tokenize the lead AND their guardians (a writing
        # answer may name a parent/guardian), mirroring run_assignment_feedback.
        # Structured PII (phones/emails/ids) is caught by the regexes in redaction.py.
        names = []
        if attempt.student.user_id and attempt.student.user is not None:
            full = attempt.student.user.get_full_name()
            if full:
                names.append(full)
        for first, last in attempt.student.guardians.select_related("parent__user").values_list(
            "parent__user__first_name", "parent__user__last_name"
        ):
            guardian = f"{first or ''} {last or ''}".strip()
            if guardian:
                names.append(guardian)
        return (
            body,
            names,
            lambda restored: apply_writing_marks(attempt_id=attempt_id, output_text=restored),
        )

    return _run_with_retry(self, ai_request_id, build_prompt=_build)


def _prompt_cap(feature: str) -> int:
    """The active prompt's token cost cap = the budget estimate (TD-13)."""
    from apps.ai.services import active_prompt

    return active_prompt(feature).token_cost_cap


# ---------------------------------------------------------------------------
# Library material generation (F9-1)
# ---------------------------------------------------------------------------


@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def run_material_generation(self, ai_request_id: int, *, params: dict | None = None) -> str | None:
    """Draft a library material's body from its title + topic; the persist hook writes
    the AI text onto the DRAFT material (the manager then reviews + publishes)."""
    params = params or {}
    material_id = int(params.get("material_id") or 0)

    def _build(prompt, request):
        from apps.content.services import apply_generated_material

        body = prompt.user_template.format(
            title=params.get("title") or "Untitled",
            topic=params.get("topic") or "the topic",
        )
        # No student PII in a material-gen prompt; persist writes the drafted body.
        return (
            body,
            [],
            lambda restored: apply_generated_material(material_id=material_id, output_text=restored),
        )

    return _run_with_retry(self, ai_request_id, build_prompt=_build)


# ---------------------------------------------------------------------------
# Message template generation (F10-2)
# ---------------------------------------------------------------------------


@app.task(bind=True, max_retries=3, retry_backoff=True, acks_late=True)
def run_template_generation(self, ai_request_id: int, *, params: dict | None = None) -> str | None:
    """Draft a reusable message template's body from its name + purpose; the persist hook
    writes the AI text onto the template (the staff edits it afterwards)."""
    params = params or {}
    template_id = int(params.get("template_id") or 0)

    def _build(prompt, request):
        from apps.campaigns.services import apply_generated_template

        body = prompt.user_template.format(
            name=params.get("name") or "Untitled",
            purpose=params.get("purpose") or "a general message",
        )
        # No student PII in a template-gen prompt; persist writes the drafted body.
        return (
            body,
            [],
            lambda restored: apply_generated_template(template_id=template_id, output_text=restored),
        )

    return _run_with_retry(self, ai_request_id, build_prompt=_build)
