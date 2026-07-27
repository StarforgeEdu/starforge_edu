"""Assignments domain signals (all emit-only today).

Consumers: D3-C notifications (`assignment_published`, `assignment_due_soon`,
`submission_graded`) and D4-A AI feedback (`ai_feedback_requested`). Flat
primitive kwargs + ``schema_name`` for cross-context dispatch.

Signatures:
    assignment_published.send(sender=Assignment, assignment_id, cohort_id, schema_name)
    assignment_due_soon.send(sender=Assignment, assignment_id, cohort_id, due_at, schema_name)
    submission_graded.send(sender=Submission, submission_id, student_id, score, schema_name)
    ai_feedback_requested.send(sender=Submission, submission_id, requested_by, schema_name)
"""

from __future__ import annotations

import django.dispatch

assignment_published = django.dispatch.Signal()
assignment_due_soon = django.dispatch.Signal()
submission_graded = django.dispatch.Signal()
ai_feedback_requested = django.dispatch.Signal()
