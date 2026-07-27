"""Placement engine services (F1-2 / F1-4): build → submit → approve / reject.

All writes are keyword-only and `@transaction.atomic`. Questions can only change
while the test is DRAFT (editing a live test would invalidate attempts already
graded against it). The approve transition locks the row (`select_for_update`) so
the state check + maker-checker self-check are race-free.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.cohorts.services import enroll_student_in_cohort
from apps.org.selectors import get_center_settings
from apps.placement.models import (
    GroupProposal,
    PlacementAnswer,
    PlacementAttempt,
    PlacementQuestion,
    PlacementTest,
)
from apps.students.models import StudentProfile
from core.exceptions import (
    ConflictException,
    PermissionException,
    UnprocessableEntity,
    ValidationException,
)

logger = logging.getLogger("starforge.placement")
_QT = PlacementQuestion.QuestionType
# Both choice types share the same options shape ([str]); they differ only in the
# correct_answer (a single option vs. a subset) and in how the response is graded.
_CHOICE_TYPES = (_QT.SINGLE_CHOICE, _QT.MULTIPLE_CHOICE)
# Placement is an intake tool: only prospective students get a test, so submit's
# academic_level write never clobbers an enrolled student's curated level.
_PROSPECTIVE_STATUSES = (
    StudentProfile.Status.LEAD,
    StudentProfile.Status.APPLICATION,
    StudentProfile.Status.ACCEPTED,
)


@transaction.atomic
def create_test(*, title: str, created_by=None, **kwargs) -> PlacementTest:
    return PlacementTest.objects.create(title=title, created_by=created_by, **kwargs)


@transaction.atomic
def update_test(*, test: PlacementTest, **changes) -> PlacementTest:
    """Edit test metadata. Draft-only — a test that is pending or live is frozen."""
    if test.status != PlacementTest.Status.DRAFT:
        raise UnprocessableEntity(_("Only a draft test can be edited."), code="test_not_draft")
    allowed = {"title", "description", "subject", "time_limit_minutes"}
    for key, value in changes.items():
        if key in allowed:
            setattr(test, key, value)
    test.save()
    return test


def _validate_question(question_type: str, options: Any, correct_answer: Any) -> None:
    """Enforce a coherent answer-key per type; raise ValidationException otherwise."""
    if not isinstance(options, list):
        # The serializer JSONField accepts any JSON; a scalar/string/dict here would
        # otherwise 500 on len() or silently "match" correct_answer by substring/key.
        raise ValidationException(_("Options must be a list."), code="invalid_options")
    if question_type in _CHOICE_TYPES:
        if len(options) < 2:
            raise ValidationException(
                _("A choice question needs at least two options."), code="choice_needs_options"
            )
        if any(not isinstance(o, str) or not o.strip() for o in options):
            raise ValidationException(_("Options must be non-empty text."), code="invalid_options")
        if len(set(options)) != len(options):
            raise ValidationException(_("Options must be unique."), code="duplicate_options")
        if question_type == _QT.SINGLE_CHOICE:
            if correct_answer not in options:
                raise ValidationException(
                    _("The correct answer must be one of the options."), code="answer_not_in_options"
                )
        else:  # MULTIPLE_CHOICE: the key is a non-empty subset of the options
            if not isinstance(correct_answer, list) or not correct_answer:
                raise ValidationException(
                    _("A multiple-choice question needs at least one correct option."),
                    code="answer_not_list",
                )
            # Check membership BEFORE set() so an unhashable junk entry (dict/list the AI
            # path might emit) raises a clean 422 instead of a TypeError 500.
            if any(a not in options for a in correct_answer):
                raise ValidationException(
                    _("Every correct answer must be one of the options."), code="answer_not_in_options"
                )
            if len(set(correct_answer)) != len(correct_answer):
                raise ValidationException(
                    _("The correct answers must be unique."), code="duplicate_answers"
                )
    elif question_type == _QT.SHORT_ANSWER:
        # The key is a non-empty list of acceptable answers (synonyms / spellings);
        # a typed response auto-grades correct if it normalizes to any of them.
        if not isinstance(correct_answer, list) or not correct_answer:
            raise ValidationException(
                _("A short-answer question needs at least one acceptable answer."),
                code="answer_not_list",
            )
        if any(not isinstance(a, str) or not a.strip() for a in correct_answer):
            raise ValidationException(
                _("Each acceptable answer must be non-empty text."), code="invalid_answers"
            )
    elif question_type == _QT.TRUE_FALSE:
        if not isinstance(correct_answer, bool):
            raise ValidationException(
                _("A true/false question's answer must be true or false."), code="answer_not_boolean"
            )
    elif question_type in PlacementQuestion.HUMAN_GRADED_TYPES:
        # writing / reading / listening / speaking are all human-marked: no answer key.
        if correct_answer is not None:
            raise ValidationException(
                _("This question is marked by a person and has no answer key."),
                code="writing_has_no_answer",
            )
    else:
        # Belt-and-suspenders for the AI path (the serializer enforces choices on the
        # manual path): an unknown type would otherwise be stored as junk or overflow
        # the varchar(16) column on bulk_create.
        raise ValidationException(_("Unknown question type."), code="invalid_question_type")


def _allowed_question_types() -> list[str]:
    """The center's enabled placement question types (F8-1). Empty = no restriction."""
    return list(get_center_settings().placement_allowed_question_types or [])


def _assert_type_allowed(question_type: str) -> None:
    allowed = _allowed_question_types()
    if allowed and question_type not in allowed:
        raise UnprocessableEntity(
            _("That question type is not enabled for this center."),
            code="question_type_not_allowed",
        )


@transaction.atomic
def add_question(
    *,
    test: PlacementTest,
    prompt: str,
    question_type: str,
    options: list[Any] | None = None,
    correct_answer: Any = None,
    points: int = 1,
    order: int | None = None,
    media: dict | None = None,
) -> PlacementQuestion:
    """Append a question. Only a DRAFT test can be edited. The parent row is locked
    so the status check and the next-slot `order` computation are race-free (two
    concurrent appends can't read the same MAX(order) and collide)."""
    test = PlacementTest.objects.select_for_update().get(pk=test.pk)
    if test.status != PlacementTest.Status.DRAFT:
        raise UnprocessableEntity(_("Only a draft test can be edited."), code="test_not_draft")
    options = options if options is not None else []
    _validate_question(question_type, options, correct_answer)
    _assert_type_allowed(question_type)  # F8-1: respect the center's enabled types
    media = _clean_media(media)
    if order is None:
        last = test.questions.order_by("-order").first()
        order = (last.order + 1) if last else 0
    return PlacementQuestion.objects.create(
        test=test,
        prompt=prompt,
        question_type=question_type,
        options=options,
        correct_answer=correct_answer,
        points=points,
        order=order,
        media=media,
    )


def _clean_media(media: Any) -> dict:
    """Media is a free-form object shown to the taker with the question (a listening audio
    URL/key, a reading passage, ...). Reject a non-object so `.media` is always a dict."""
    if media is None:
        return {}
    if not isinstance(media, dict):
        raise ValidationException(_("media must be an object."), code="invalid_media")
    return media


@transaction.atomic
def remove_question(*, question: PlacementQuestion) -> None:
    """Drop a question. Only while the test is DRAFT."""
    if question.test.status != PlacementTest.Status.DRAFT:
        raise UnprocessableEntity(_("Only a draft test can be edited."), code="test_not_draft")
    question.delete()


@transaction.atomic
def submit_for_review(*, test: PlacementTest) -> PlacementTest:
    """DRAFT → PENDING. A test must have at least one question to be reviewable."""
    if test.status != PlacementTest.Status.DRAFT:
        raise UnprocessableEntity(
            _("Only a draft test can be submitted for review."), code="test_not_draft"
        )
    if not test.questions.exists():
        raise UnprocessableEntity(
            _("Add at least one question before submitting."), code="test_has_no_questions"
        )
    test.status = PlacementTest.Status.PENDING
    test.submitted_at = timezone.now()
    test.reject_reason = ""
    test.save(update_fields=["status", "submitted_at", "reject_reason", "updated_at"])
    return test


@transaction.atomic
def approve_test(*, test: PlacementTest, approver) -> PlacementTest:
    """PENDING → APPROVED. Maker-checker: the approver must be a different person
    than the builder. The row is locked so the state + self-check and the write
    can't race a concurrent approval."""
    test = PlacementTest.objects.select_for_update().get(pk=test.pk)
    if test.status != PlacementTest.Status.PENDING:
        raise UnprocessableEntity(
            _("Only a test pending review can be approved."), code="test_not_pending"
        )
    if test.created_by_id is not None and test.created_by_id == approver.id:
        raise PermissionException(
            _("You cannot approve a placement test you built yourself."), code="self_approval"
        )
    test.status = PlacementTest.Status.APPROVED
    test.approved_by = approver
    test.approved_at = timezone.now()
    test.reject_reason = ""
    test.save(update_fields=["status", "approved_by", "approved_at", "reject_reason", "updated_at"])
    return test


@transaction.atomic
def reject_test(*, test: PlacementTest, reviewer, reason: str) -> PlacementTest:
    """PENDING → DRAFT, kicked back to the builder with a reason so it can be fixed
    and re-submitted."""
    test = PlacementTest.objects.select_for_update().get(pk=test.pk)
    if test.status != PlacementTest.Status.PENDING:
        raise UnprocessableEntity(
            _("Only a test pending review can be rejected."), code="test_not_pending"
        )
    test.status = PlacementTest.Status.DRAFT
    test.submitted_at = None
    test.reject_reason = reason
    test.save(update_fields=["status", "submitted_at", "reject_reason", "updated_at"])
    return test


@transaction.atomic
def delete_test(*, test: PlacementTest) -> None:
    """Hard-delete a test. Only a DRAFT may be deleted — a test that is pending or
    approved is an accountability artifact (it carries the checker's sign-off) and
    is never erased unilaterally. The row is locked against a concurrent submit."""
    test = PlacementTest.objects.select_for_update().get(pk=test.pk)
    if test.status != PlacementTest.Status.DRAFT:
        raise UnprocessableEntity(
            _("Only a draft test can be deleted; submit a rejection instead."), code="test_not_draft"
        )
    test.delete()


# ---------------------------------------------------------------------------
# Sitting + auto-grading (F1-5 / F1-6)
# ---------------------------------------------------------------------------


def _level_for(score: int, max_score: int) -> str:
    """Transparent rubric: band by share of the OBJECTIVE points. No objective
    questions (max_score == 0) → no auto-level (a human marks the writing, F8-3)."""
    if max_score <= 0:
        return ""
    pct = score / max_score
    if pct >= 0.7:
        return "advanced"
    if pct >= 0.4:
        return "intermediate"
    return "beginner"


def _validate_response(question: PlacementQuestion, response: Any) -> None:
    """A wrong answer is fine (it scores 0); a wrong-TYPED answer is a clean 400 so
    we never store junk."""

    def bad(msg, code: str):
        return ValidationException(msg, code=code, fields={"question": [str(question.id)]})

    if question.question_type == _QT.TRUE_FALSE:
        if not isinstance(response, bool):
            raise bad(_("Answer true or false."), "answer_not_boolean")
    elif question.question_type == _QT.MULTIPLE_CHOICE:
        # A list of chosen options (an empty list = "left blank", which scores 0).
        if not isinstance(response, list) or any(not isinstance(r, str) for r in response):
            raise bad(_("Answer must be a list of options."), "answer_not_list")
        if any(r not in question.options for r in response):
            raise bad(_("Answer must be one of the options."), "answer_not_in_options")
    elif not isinstance(response, str):  # single_choice + writing are text
        raise bad(_("Answer must be text."), "answer_not_text")
    elif question.question_type == _QT.SINGLE_CHOICE and response not in question.options:
        # A wrong answer is fine (it scores 0); an answer that was never an offered
        # option is junk — reject it cleanly, matching the forms engine.
        raise bad(_("Answer must be one of the options."), "answer_not_in_options")


def _normalize(text: str) -> str:
    """Fold a typed short answer for comparison: NFC-normalize, casefold, then collapse
    all whitespace. A transparent, forgiving rule — case, Unicode composition form, and
    spacing don't matter (so an accented Uzbek/Russian answer typed in a different
    keyboard normal form isn't mis-graded), but it is NOT fuzzy matching, so what counts
    as correct stays predictable for staff and leads alike."""
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _grade_answer(question: PlacementQuestion, response: Any) -> tuple[bool | None, int]:
    """(is_correct, awarded_points). Writing is marked by a person later → (None, 0).
    Multiple-choice is all-or-nothing: the chosen SET must equal the correct set
    exactly. Short-answer matches the normalized response against any acceptable answer.
    `_validate_response` has already type-checked `response`, so set()/_normalize are safe."""
    if question.question_type not in PlacementQuestion.AUTO_GRADED_TYPES:
        return None, 0
    if question.question_type == _QT.MULTIPLE_CHOICE:
        is_correct = isinstance(response, list) and set(response) == set(question.correct_answer or [])
    elif question.question_type == _QT.SHORT_ANSWER:
        accepted = {_normalize(a) for a in (question.correct_answer or [])}
        is_correct = isinstance(response, str) and _normalize(response) in accepted
    else:
        is_correct = response == question.correct_answer
    return is_correct, (question.points if is_correct else 0)


@transaction.atomic
def assign_test(*, test: PlacementTest, student, assigned_by=None) -> PlacementAttempt:
    """Give an approved test to a prospective student. One attempt per (test, student)."""
    if test.status != PlacementTest.Status.APPROVED:
        raise UnprocessableEntity(
            _("Only an approved test can be assigned."), code="test_not_approved"
        )
    if student.status not in _PROSPECTIVE_STATUSES:
        raise UnprocessableEntity(
            _("Placement tests are only for prospective students."), code="student_not_prospective"
        )
    if PlacementAttempt.objects.filter(test=test, student=student).exists():
        raise ConflictException(
            _("This student already has this placement test."), code="already_assigned"
        )
    # F8-2 timer: a timed test gives the lead a fixed window from assignment.
    expires_at = None
    if test.time_limit_minutes:
        expires_at = timezone.now() + timedelta(minutes=test.time_limit_minutes)
    try:
        # Savepoint so a racing duplicate hits the unique constraint as a clean 409,
        # not a 500 (mirrors apps/forms submit_response).
        with transaction.atomic():
            return PlacementAttempt.objects.create(
                test=test, student=student, assigned_by=assigned_by, expires_at=expires_at
            )
    except IntegrityError:
        raise ConflictException(
            _("This student already has this placement test."), code="already_assigned"
        ) from None


@transaction.atomic
def submit_attempt(*, attempt: PlacementAttempt, answers: list[dict]) -> PlacementAttempt:
    """Record + auto-grade a lead's answers, set the level on their profile, and
    freeze the attempt. The row is locked so it can't be double-submitted."""
    # of=("self",): lock ONLY the attempt row. A bare FOR UPDATE with select_related
    # JOINs also locks the joined PlacementTest + StudentProfile rows on Postgres —
    # PlacementTest is a SHARED parent (one approved test, many attempts), so every
    # lead's concurrent submit would serialize on that one row (and 500 on a
    # lock_timeout, unmapped by middleware). We only need to guard double-submit.
    attempt = (
        PlacementAttempt.objects.select_for_update(of=("self",))
        .select_related("test", "student")
        .get(pk=attempt.pk)
    )
    if attempt.status != PlacementAttempt.Status.ASSIGNED:
        raise ConflictException(
            _("This placement attempt has already been submitted."), code="already_submitted"
        )
    if attempt.expires_at is not None and timezone.now() > attempt.expires_at:
        raise UnprocessableEntity(
            _("The time limit for this placement test has passed."), code="attempt_expired"
        )
    questions = {q.id: q for q in attempt.test.questions.all()}
    seen: set[int] = set()
    rows: list[PlacementAnswer] = []
    for item in answers:
        qid = item.get("question")
        if not isinstance(qid, int) or qid not in questions:
            raise ValidationException(
                _("Unknown question for this test."),
                code="unknown_question",
                fields={"question": [str(qid)]},
            )
        if qid in seen:
            raise ValidationException(_("Duplicate answer for a question."), code="duplicate_answer")
        seen.add(qid)
        question = questions[qid]
        response = item.get("response")
        _validate_response(question, response)
        is_correct, awarded = _grade_answer(question, response)
        rows.append(
            PlacementAnswer(
                attempt=attempt,
                question=question,
                response=response,
                is_correct=is_correct,
                awarded_points=awarded,
            )
        )
    PlacementAnswer.objects.bulk_create(rows)
    attempt.status = PlacementAttempt.Status.GRADED
    attempt.submitted_at = timezone.now()
    attempt.save(update_fields=["status", "submitted_at", "updated_at"])
    _grade_attempt(attempt)  # objective now; writing counts once marked (F8-3)
    return attempt


def _grade_attempt(attempt: PlacementAttempt) -> None:
    """Recompute score/max_score/level over the attempt's answers and push the level
    to the lead's profile (F1-6). An objective question always counts toward the
    denominator; a WRITING question counts only once it has been marked (is_correct
    set) — so this is identical to objective-only grading at submit time, and folds
    in the writing marks after F8-3 marking. The single source of truth for the grade."""
    questions = attempt.test.questions.all()
    answers = {a.question_id: a for a in attempt.answers.all()}
    score = 0
    max_score = 0
    for q in questions:
        answer = answers.get(q.id)
        objective = q.question_type in PlacementQuestion.AUTO_GRADED_TYPES
        marked_writing = answer is not None and answer.is_correct is not None
        if objective or marked_writing:
            max_score += q.points
            if answer is not None:
                score += answer.awarded_points
    attempt.score = score
    attempt.max_score = max_score
    attempt.level = _level_for(score, max_score)
    attempt.save(update_fields=["score", "max_score", "level", "updated_at"])
    # Push the level to the lead's profile ONLY while they are still prospective — the
    # marking path can run after the student has been enrolled + their academic_level
    # hand-curated, and must not clobber it (same invariant the assign-gate enforces).
    if attempt.level and attempt.student.status in _PROSPECTIVE_STATUSES:
        attempt.student.academic_level = attempt.level
        attempt.student.save(update_fields=["academic_level", "updated_at"])


# ---------------------------------------------------------------------------
# Group placement: propose → accept / reject (F1-8)
# ---------------------------------------------------------------------------


def _assert_proposable(student, cohort) -> None:
    if student.status not in _PROSPECTIVE_STATUSES:
        raise UnprocessableEntity(
            _("Only a prospective student can be placed into a group."), code="student_not_prospective"
        )
    if cohort.is_archived:
        raise ValidationException(_("That group is archived."), code="cohort_archived")
    if student.branch_id != cohort.branch_id:
        raise ValidationException(
            _("The student and the group are in different branches."), code="student_branch_mismatch"
        )


def _finalize_accept(proposal: GroupProposal, *, decided_by) -> None:
    """Enroll the lead into the proposed cohort and mark the proposal accepted."""
    membership = enroll_student_in_cohort(cohort=proposal.cohort, student=proposal.student)
    proposal.status = GroupProposal.Status.ACCEPTED
    proposal.decided_by = decided_by
    proposal.decided_at = timezone.now()
    proposal.membership = membership
    proposal.save(update_fields=["status", "decided_by", "decided_at", "membership", "updated_at"])


@transaction.atomic
def propose_group(*, student, cohort, proposed_by) -> GroupProposal:
    """Reception proposes a cohort for a placed lead. If the centre does not require
    a manager's acceptance, the proposal auto-accepts and enrolls immediately."""
    _assert_proposable(student, cohort)
    if GroupProposal.objects.filter(
        student=student, cohort=cohort, status=GroupProposal.Status.PENDING
    ).exists():
        raise ConflictException(
            _("This student already has a pending proposal for this group."), code="already_proposed"
        )
    try:
        # Savepoint so a racing duplicate hits the partial unique constraint as a
        # clean 409, not a 500 (mirrors assign_test / enroll_student_in_cohort).
        with transaction.atomic():
            proposal = GroupProposal.objects.create(
                student=student, cohort=cohort, proposed_by=proposed_by
            )
    except IntegrityError:
        raise ConflictException(
            _("This student already has a pending proposal for this group."), code="already_proposed"
        ) from None
    if not get_center_settings().require_group_acceptance:
        # Toggle off: reception assigns directly — accept + enroll now (no second sign-off).
        _finalize_accept(proposal, decided_by=proposed_by)
    return proposal


@transaction.atomic
def accept_proposal(*, proposal: GroupProposal, manager) -> GroupProposal:
    """A manager accepts a pending proposal → the lead is enrolled. Maker-checker:
    the manager must differ from the proposer (the centre turned acceptance on
    precisely to get a second pair of eyes). The row is locked against a race."""
    proposal = (
        GroupProposal.objects.select_for_update().select_related("student", "cohort").get(pk=proposal.pk)
    )
    if proposal.status != GroupProposal.Status.PENDING:
        raise UnprocessableEntity(
            _("Only a pending proposal can be accepted."), code="proposal_not_pending"
        )
    if proposal.proposed_by_id is not None and proposal.proposed_by_id == manager.id:
        raise PermissionException(
            _("You cannot accept a group proposal you made yourself."), code="self_acceptance"
        )
    # Re-assert the propose-time invariants at decision time: a proposal can sit
    # PENDING while the lead's status/branch or the cohort drifts (symmetric paths).
    _assert_proposable(proposal.student, proposal.cohort)
    _finalize_accept(proposal, decided_by=manager)
    return proposal


@transaction.atomic
def reject_proposal(*, proposal: GroupProposal, manager, reason: str) -> GroupProposal:
    """A manager rejects a pending proposal with a reason. No enrollment happens."""
    proposal = GroupProposal.objects.select_for_update().get(pk=proposal.pk)
    if proposal.status != GroupProposal.Status.PENDING:
        raise UnprocessableEntity(
            _("Only a pending proposal can be rejected."), code="proposal_not_pending"
        )
    proposal.status = GroupProposal.Status.REJECTED
    proposal.decided_by = manager
    proposal.decided_at = timezone.now()
    proposal.reject_reason = reason
    proposal.save(update_fields=["status", "decided_by", "decided_at", "reject_reason", "updated_at"])
    return proposal


# ---------------------------------------------------------------------------
# AI question generation (F1-3) — reuses the apps.ai budget/redaction pipeline
# ---------------------------------------------------------------------------

_MAX_GENERATED_QUESTIONS = 100
_MAX_QUESTION_POINTS = 100  # sane domain cap, well under the smallint column limit


def request_placement_generation(
    *, test: PlacementTest, count: int, difficulty: str = "medium", topic: str = "", requested_by=None
):
    """Ask the AI to draft questions for a DRAFT test. Gated by the centre's AI-
    generation toggle, budget-reserved, and enqueued on commit; the generated
    questions are applied to the draft by the task (apply_generated_questions)."""
    from apps.ai.models import AIFeature
    from apps.ai.services import AIFeatureDisabled, active_prompt, check_and_reserve_budget
    from core.utils import current_schema

    if test.status != PlacementTest.Status.DRAFT:
        raise UnprocessableEntity(
            _("Questions can only be generated for a draft test."), code="test_not_draft"
        )
    if not get_center_settings().ai_exam_generation_enabled:
        raise AIFeatureDisabled(code="feature_disabled")

    prompt = active_prompt(AIFeature.PLACEMENT_GENERATION)
    ai_request = check_and_reserve_budget(
        feature=AIFeature.PLACEMENT_GENERATION,
        estimated_tokens=prompt.token_cost_cap,
        requested_by=requested_by,
        source_app="placement",
        source_id=test.id,
    )
    if getattr(ai_request, "_should_enqueue", False):
        schema = current_schema()
        params = {
            "test_id": test.id,
            "count": count,
            "difficulty": difficulty,
            "topic": topic,
        }
        transaction.on_commit(lambda: _enqueue_placement_generation(ai_request.pk, params, schema))
    return ai_request


def _enqueue_placement_generation(ai_request_id: int, params: dict, schema: str) -> None:
    from celery_tasks.ai_tasks import run_placement_generation

    run_placement_generation.delay(ai_request_id, params=params, _schema_name=schema)


def _parse_question_payload(output_text: str) -> list:
    """Best-effort: extract the JSON array of questions, tolerating ``` fences."""
    text = (output_text or "").strip()
    if text.startswith("```"):
        inner = text[3:]
        if inner[:4].lower() == "json":
            inner = inner[4:]
        text = inner.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        # Untrusted model output: a malformed or pathologically-nested payload must
        # never escape this parser (the apply hook is tolerant-by-design).
        logger.warning("placement gen: AI output was not valid JSON")
        return []
    return data if isinstance(data, list) else []


@transaction.atomic
def apply_generated_questions(*, test_id: int, output_text: str) -> int:
    """Parse the AI's JSON question array and append the VALID ones to the DRAFT
    test. Tolerant by design: a malformed payload or an individual bad question is
    skipped, never raised — so a partial result still applies and the AIRequest still
    succeeds (the manager reviews + submits the draft as usual). Returns count added."""
    try:
        test = PlacementTest.objects.select_for_update().get(pk=test_id)
    except PlacementTest.DoesNotExist:
        return 0  # the draft was deleted between the request and the task finishing
    if test.status != PlacementTest.Status.DRAFT:
        # The test left DRAFT between the request and the task finishing — never
        # mutate a pending/approved test (it may already be in review or live).
        return 0
    last = test.questions.order_by("-order").first()
    order = (last.order + 1) if last else 0
    # Dedup against existing prompts so a task RETRY (which re-runs the persist hook
    # after a mid-run failure) can't double-apply the same questions, and a model
    # that repeats a question within one batch only adds it once.
    seen_prompts = set(test.questions.values_list("prompt", flat=True))
    allowed = _allowed_question_types()  # F8-1: skip types the center hasn't enabled
    rows: list[PlacementQuestion] = []
    for item in _parse_question_payload(output_text)[:_MAX_GENERATED_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        prompt_text = item.get("prompt")
        question_type = item.get("question_type")
        if not isinstance(prompt_text, str) or not prompt_text.strip():
            continue
        if not isinstance(question_type, str) or prompt_text in seen_prompts:
            continue
        if allowed and question_type not in allowed:
            continue  # the model proposed a type this center has disabled — drop it
        options = item.get("options") or []
        correct = (
            None
            if question_type in PlacementQuestion.HUMAN_GRADED_TYPES
            else item.get("correct_answer")
        )
        # Coerce a stringified boolean the model may emit for true/false.
        if (
            question_type == _QT.TRUE_FALSE
            and isinstance(correct, str)
            and correct.strip().lower() in ("true", "false")
        ):
            correct = correct.strip().lower() == "true"
        points = item.get("points", 1)
        if not isinstance(points, int) or isinstance(points, bool) or points < 1:
            points = 1
        points = min(points, _MAX_QUESTION_POINTS)  # bound it (smallint column)
        try:
            _validate_question(question_type, options, correct)
        except (ValidationException, UnprocessableEntity):
            continue  # skip a question the model got wrong; keep the good ones
        seen_prompts.add(prompt_text)
        rows.append(
            PlacementQuestion(
                test=test,
                prompt=prompt_text,
                question_type=question_type,
                options=options if question_type in _CHOICE_TYPES else [],
                correct_answer=correct,
                points=points,
                order=order,
            )
        )
        order += 1
    if rows:
        PlacementQuestion.objects.bulk_create(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# AI marking of writing answers (F8-3) — reuses the apps.ai pipeline + _grade_attempt
# ---------------------------------------------------------------------------


def request_writing_marking(*, attempt: PlacementAttempt, requested_by=None):
    """Ask the AI to score the WRITING answers of a submitted attempt. Budget-reserved
    and enqueued on commit; the marks are applied + the grade recomputed by the task."""
    from apps.ai.models import AIFeature
    from apps.ai.services import active_prompt, check_and_reserve_budget
    from core.utils import current_schema

    if attempt.status != PlacementAttempt.Status.GRADED:
        raise UnprocessableEntity(
            _("Only a submitted attempt can be marked."), code="attempt_not_graded"
        )
    writing_qids = set(
        attempt.test.questions.filter(question_type=_QT.WRITING).values_list("id", flat=True)
    )
    if not writing_qids or not attempt.answers.filter(question_id__in=writing_qids).exists():
        raise UnprocessableEntity(
            _("This attempt has no writing answers to mark."), code="no_writing_answers"
        )
    prompt = active_prompt(AIFeature.WRITING_MARKING)
    ai_request = check_and_reserve_budget(
        feature=AIFeature.WRITING_MARKING,
        estimated_tokens=prompt.token_cost_cap,
        requested_by=requested_by,
        source_app="placement",
        source_id=attempt.id,
    )
    if getattr(ai_request, "_should_enqueue", False):
        schema = current_schema()
        transaction.on_commit(lambda: _enqueue_writing_marking(ai_request.pk, attempt.id, schema))
    return ai_request


def _enqueue_writing_marking(ai_request_id: int, attempt_id: int, schema: str) -> None:
    from celery_tasks.ai_tasks import run_writing_marking

    run_writing_marking.delay(ai_request_id, params={"attempt_id": attempt_id}, _schema_name=schema)


@transaction.atomic
def apply_writing_marks(*, attempt_id: int, output_text: str) -> int:
    """Parse the AI's per-question scores and apply them to the attempt's WRITING
    answers, then recompute the grade (F8-3). Tolerant: malformed / unmatched / out-of-
    range items are skipped, never raised; idempotent — re-running OVERWRITES the same
    answers' marks (a retry can't double-count). Returns the count of answers marked."""
    try:
        attempt = (
            PlacementAttempt.objects.select_for_update(of=("self",))
            .select_related("test", "student")
            .get(pk=attempt_id)
        )
    except PlacementAttempt.DoesNotExist:
        return 0
    if attempt.status != PlacementAttempt.Status.GRADED:
        return 0
    writing_answers = {
        a.question_id: a
        for a in attempt.answers.select_related("question").filter(
            question__question_type=_QT.WRITING
        )
    }
    if not writing_answers:
        return 0
    marked = 0
    for item in _parse_question_payload(output_text):
        if not isinstance(item, dict):
            continue
        qid = item.get("question_id")
        score = item.get("score")
        # bool is an int subclass (True == 1); reject it on BOTH fields so a JSON
        # `true` can't be coerced to question_id 1 or a score of 1.
        if not isinstance(qid, int) or isinstance(qid, bool) or qid not in writing_answers:
            continue
        if not isinstance(score, int) or isinstance(score, bool):
            continue
        answer = writing_answers[qid]
        awarded = max(0, min(score, answer.question.points))  # clamp to [0, points]
        answer.awarded_points = awarded
        answer.is_correct = awarded > 0  # mark it (no longer null) so it counts in the grade
        answer.save(update_fields=["awarded_points", "is_correct"])
        marked += 1
    if marked:
        _grade_attempt(attempt)
    return marked


@transaction.atomic
def mark_writing_manually(*, attempt: PlacementAttempt, marks: list[dict]) -> PlacementAttempt:
    """A human marker scores the WRITING answers directly — no AI, no budget — for a
    center that marks by hand or has exhausted its AI budget. Same effect as
    apply_writing_marks (set awarded_points + is_correct so writing counts, then recompute
    the grade), but STRICT: a person's input is validated, not tolerated like untrusted
    model output. An unknown / duplicate / non-writing question, or an out-of-range score,
    is a clean 4xx — never silently skipped or clamped. Idempotent: re-marking a question
    overwrites its previous mark; partial marking is allowed (unmarked writing stays
    uncounted). The row is locked so two markers can't race."""
    attempt = (
        PlacementAttempt.objects.select_for_update(of=("self",))
        .select_related("test", "student")
        .get(pk=attempt.pk)
    )
    if attempt.status != PlacementAttempt.Status.GRADED:
        raise UnprocessableEntity(
            _("Only a submitted attempt can be marked."), code="attempt_not_graded"
        )
    # Any human-marked type (writing + the reading/listening/speaking media skills).
    writing_answers = {
        a.question_id: a
        for a in attempt.answers.select_related("question").filter(
            question__question_type__in=PlacementQuestion.HUMAN_GRADED_TYPES
        )
    }
    if not writing_answers:
        raise UnprocessableEntity(
            _("This attempt has no manually-marked answers to mark."), code="no_writing_answers"
        )
    seen: set[int] = set()
    updates: list[PlacementAnswer] = []
    for item in marks:
        qid = item.get("question")
        score = item.get("score")
        # bool is an int subclass — reject it so JSON `true` can't pose as an id or a score.
        if not isinstance(qid, int) or isinstance(qid, bool) or qid not in writing_answers:
            raise ValidationException(
                _("Unknown writing question for this attempt."),
                code="unknown_writing_question",
                fields={"question": [str(qid)]},
            )
        if qid in seen:
            raise ValidationException(_("Duplicate mark for a question."), code="duplicate_mark")
        seen.add(qid)
        if not isinstance(score, int) or isinstance(score, bool) or score < 0:
            raise ValidationException(
                _("A score must be a whole number of zero or more."),
                code="invalid_score",
                fields={"question": [str(qid)]},
            )
        answer = writing_answers[qid]
        if score > answer.question.points:
            # A human's mark above the maximum is a mistake, not something to silently
            # clamp (unlike the tolerant AI path) — surface it so they can correct it.
            raise ValidationException(
                _("The score may not exceed the question's points."),
                code="score_out_of_range",
                fields={"question": [str(qid)]},
            )
        answer.awarded_points = score
        answer.is_correct = score > 0
        updates.append(answer)
    for answer in updates:
        answer.save(update_fields=["awarded_points", "is_correct"])
    _grade_attempt(attempt)
    return attempt
