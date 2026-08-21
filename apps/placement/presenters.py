"""Plain dict presenters for the placement app (off DRF).

Replace the DRF serializers. THE ANSWER-KEY GATE lives here (two layers):
  * a question nested inside an ATTEMPT is always key-free (attempt_question_to_dict,
    no correct_answer) so a test-taker never receives the key;
  * an attempt's answers are staff-full (placement_answer_to_dict, with is_correct/
    awarded_points) vs lead-narrow (lead_answer_to_dict, {question, response}) — a lead
    must not infer the key from per-question correctness.
The full question view WITH correct_answer (placement_question_to_dict) is used ONLY on
the test endpoints, which are placement-staff-only.
"""

from __future__ import annotations

from typing import Any

from apps.placement.models import (
    GroupProposal,
    PlacementAnswer,
    PlacementAttempt,
    PlacementQuestion,
    PlacementTest,
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def placement_question_to_dict(q: PlacementQuestion) -> dict[str, Any]:
    """Full question WITH the answer key — staff-only (test endpoints)."""
    return {
        "id": q.id,
        "prompt": q.prompt,
        "question_type": q.question_type,
        "options": q.options,
        "media": q.media,
        "correct_answer": q.correct_answer,
        "points": q.points,
        "order": q.order,
    }


def attempt_question_to_dict(q: PlacementQuestion) -> dict[str, Any]:
    """Key-free question — what a test-taker sees while sitting (no correct_answer). `media`
    IS included: the audio/passage is part of the question the taker must answer."""
    return {
        "id": q.id,
        "prompt": q.prompt,
        "question_type": q.question_type,
        "options": q.options,
        "media": q.media,
        "points": q.points,
        "order": q.order,
    }


def placement_test_to_dict(test: PlacementTest) -> dict[str, Any]:
    return {
        "id": test.id,
        "title": test.title,
        "description": test.description,
        "status": test.status,
        "subject": test.subject_id,
        "branch": test.branch_id,
        "created_by": test.created_by_id,
        "submitted_at": _iso(test.submitted_at),
        "approved_by": test.approved_by_id,
        "approved_at": _iso(test.approved_at),
        "reject_reason": test.reject_reason,
        "time_limit_minutes": test.time_limit_minutes,
        "created_at": _iso(test.created_at),
        "questions": [placement_question_to_dict(q) for q in test.questions.all()],
    }


def placement_answer_to_dict(a: PlacementAnswer) -> dict[str, Any]:
    """Staff view — includes per-question correctness + awarded points."""
    return {
        "question": a.question_id,
        "response": a.response,
        "is_correct": a.is_correct,
        "awarded_points": a.awarded_points,
    }


def lead_answer_to_dict(a: PlacementAnswer) -> dict[str, Any]:
    """Test-taker view — response only, NO is_correct (which would leak the key)."""
    return {
        "question": a.question_id,
        "response": a.response,
    }


def placement_attempt_to_dict(attempt: PlacementAttempt, *, staff_view: bool) -> dict[str, Any]:
    answer_fn = placement_answer_to_dict if staff_view else lead_answer_to_dict
    return {
        "id": attempt.id,
        "test": attempt.test_id,
        "test_title": attempt.test.title,
        "student": attempt.student_id,
        "status": attempt.status,
        "score": attempt.score,
        "max_score": attempt.max_score,
        "level": attempt.level,
        "expires_at": _iso(attempt.expires_at),
        "submitted_at": _iso(attempt.submitted_at),
        "created_at": _iso(attempt.created_at),
        # questions are ALWAYS key-free (even for staff) — the key is only on the test.
        "questions": [attempt_question_to_dict(q) for q in attempt.test.questions.all()],
        "answers": [answer_fn(a) for a in attempt.answers.all()],
    }


def group_proposal_to_dict(p: GroupProposal) -> dict[str, Any]:
    return {
        "id": p.id,
        "student": p.student_id,
        "cohort": p.cohort_id,
        "status": p.status,
        "proposed_by": p.proposed_by_id,
        "decided_by": p.decided_by_id,
        "decided_at": _iso(p.decided_at),
        "reject_reason": p.reject_reason,
        "membership": p.membership_id,
        "created_at": _iso(p.created_at),
    }
