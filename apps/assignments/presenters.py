"""Assignment-domain presenters — plain dict mappers (replace the DRF serializers)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apps.assignments.models import Assignment, Submission, SubmissionGrade

_CENTS = Decimal("0.01")


def _money(value) -> str | None:
    # Match DRF's DecimalField(decimal_places=2) string output ("100.00").
    return str(value.quantize(_CENTS)) if value is not None else None


def assignment_to_dict(a: Assignment) -> dict[str, Any]:
    return {
        "id": a.id,
        "cohort": a.cohort_id,
        "cohort_name": a.cohort.name,
        "title": a.title,
        "description": a.description,
        "due_at": a.due_at.isoformat() if a.due_at else None,
        "attachments": a.attachments,
        "rubric": a.rubric,
        "max_score": _money(a.max_score),
        "max_resubmits": a.max_resubmits,
        "status": a.status,
        "published_at": a.published_at.isoformat() if a.published_at else None,
        "created_at": a.created_at.isoformat(),
    }


def grade_to_dict(g: SubmissionGrade) -> dict[str, Any]:
    # The auto AI-feedback pipeline creates a SubmissionGrade placeholder with score=0
    # and graded_by=None the moment a submission arrives (score is NOT-NULL), purely to
    # carry ai_feedback. That placeholder must NOT surface as an official 0.00 mark to
    # the student. graded_by=None => not human-graded => the score is a placeholder, so
    # present score/graded_at as null and flag graded=False; the advisory ai_feedback
    # still shows. A teacher legitimately grading 0 sets graded_by, so real zeros stand.
    human_graded = g.graded_by_id is not None
    return {
        "submission": g.submission_id,
        "score": _money(g.score) if human_graded else None,
        "graded": human_graded,
        "rubric_scores": g.rubric_scores,
        "feedback": g.feedback,
        "ai_feedback": g.ai_feedback,
        "graded_by": g.graded_by_id,
        "graded_at": g.graded_at.isoformat() if (human_graded and g.graded_at) else None,
    }


def submission_to_dict(s: Submission) -> dict[str, Any]:
    try:
        grade = grade_to_dict(s.grade)
    except SubmissionGrade.DoesNotExist:
        grade = None
    return {
        "id": s.id,
        "assignment": s.assignment_id,
        # Self-describing: which assignment, its deadline, and who turned it in — so a
        # submission answers "when was it due / was it late / which attempt" on its own.
        "assignment_title": s.assignment.title,
        "assignment_due_at": s.assignment.due_at.isoformat() if s.assignment.due_at else None,
        "student": s.student_id,
        "student_name": s.student.get_full_name(),
        "text": s.text,
        "attachments": s.attachments,
        "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
        "is_late": s.is_late,
        "attempt_number": s.attempt_number,
        "status": s.status,
        "grade": grade,
    }
