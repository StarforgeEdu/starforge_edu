"""Assignments write services (TASKS §12, TD-13).

Domain functions live here (imported by the layered services in ``services/v1`` AND
externally: ``submit`` by apps.ai tests, ``emit_due_soon_reminders`` by the celery beat
task). Attachment uploads are presigned (never proxied); submissions compute their late
flag + attempt number from `CenterSettings` knobs; grading validates the rubric.
Emit-only — no sms/email/push/anthropic import anywhere in this app (D3-C
notifications + D4-A AI feedback consume the signals).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import PurePosixPath

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.assignments.models import Assignment, Submission, SubmissionGrade
from apps.assignments.signals import (
    ai_feedback_requested,
    assignment_due_soon,
    assignment_published,
    submission_graded,
)
from apps.cohorts.models import CohortMembership
from apps.org.selectors import get_center_settings
from core.exceptions import ConflictException, UnprocessableEntity
from core.utils import current_schema
from infrastructure.storage.s3_client import presign_upload

# ---------------------------------------------------------------------------
# Attachment upload (presigned PUT)
# ---------------------------------------------------------------------------


def validate_and_presign_upload(*, filename: str, content_type: str, size_bytes: int) -> dict:
    """Validate against the `allowed_file_types` / `max_upload_mb` knobs (TD-13)
    and return a presigned PUT URL + the tenant-prefixed key."""
    settings = get_center_settings()
    # Sanitize to a basename: a filename containing '/' / '\' / '..' would
    # otherwise escape the per-upload {uuid}/ isolation when interpolated into
    # the key (still within {schema}/assignments/, but not cross-tenant).
    filename = PurePosixPath(filename.replace("\\", "/")).name
    if not filename or filename in {".", ".."}:
        raise UnprocessableEntity(
            _("That filename is not allowed."),
            code="invalid_filename",
            fields={"filename": ["Filename must be a non-empty basename."]},
        )
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in {e.lower() for e in settings.allowed_file_types}:
        raise UnprocessableEntity(
            _("That file type is not allowed."),
            code="file_type_not_allowed",
            fields={"filename": [f"Extension '.{ext}' is not in the allowed list."]},
        )
    # Declared content-type must be consistent with the extension (mirrors the
    # content app), closing the content-type-confusion gap for known types. Types
    # with no canonical mapping are not constrained here (the upload is still
    # presigned for that exact content_type).
    from apps.content.services import _EXT_MIME

    expected = _EXT_MIME.get(ext)
    if expected is not None and content_type not in expected:
        raise UnprocessableEntity(
            _("The declared content type does not match the file extension."),
            code="content_type_mismatch",
            fields={"content_type": [f"'{content_type}' is not valid for '.{ext}'."]},
        )
    if size_bytes > settings.max_upload_mb * 1024 * 1024:
        raise UnprocessableEntity(
            _("That file is too large."),
            code="file_too_large",
            fields={"size_bytes": [f"Exceeds the {settings.max_upload_mb} MB limit."]},
        )
    key = f"{current_schema()}/assignments/{uuid.uuid4().hex}/{filename}"
    url = presign_upload(key, content_type=content_type)
    return {"url": url, "key": key}


# ---------------------------------------------------------------------------
# Assignment lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def publish_assignment(*, assignment: Assignment, actor=None) -> Assignment:
    # Only DRAFT -> PUBLISHED. Publishing an already-PUBLISHED assignment is a
    # no-op; a CLOSED assignment must NOT silently reopen + re-emit
    # assignment_published (which would re-notify students — D3-C consumer).
    if assignment.status == Assignment.Status.PUBLISHED:
        return assignment
    if assignment.status != Assignment.Status.DRAFT:
        raise UnprocessableEntity(
            _("Only a draft assignment can be published."),
            code="assignment_not_draft",
            fields={"status": [f"Cannot publish from status '{assignment.status}'."]},
        )
    assignment.status = Assignment.Status.PUBLISHED
    assignment.published_at = timezone.now()
    assignment.save(update_fields=["status", "published_at", "updated_at"])
    schema = current_schema()
    transaction.on_commit(
        lambda: assignment_published.send(
            sender=Assignment,
            assignment_id=assignment.pk,
            cohort_id=assignment.cohort_id,
            schema_name=schema,
        )
    )
    return assignment


@transaction.atomic
def submit(
    *, assignment: Assignment, student, text: str = "", attachment_keys=None, actor=None
) -> Submission:
    """Create a submission. Rejects draft/closed assignments, non-members, and
    attempts past the resubmit limit — each with its own 422 code."""
    if assignment.status == Assignment.Status.CLOSED:
        raise UnprocessableEntity(_("This assignment is closed."), code="assignment_closed")
    if assignment.status != Assignment.Status.PUBLISHED:
        raise UnprocessableEntity(
            _("This assignment is not open for submissions."), code="assignment_not_published"
        )
    if not CohortMembership.objects.filter(
        cohort_id=assignment.cohort_id, student=student, end_date__isnull=True
    ).exists():
        raise UnprocessableEntity(
            _("You are not an active member of this assignment's cohort."),
            code="student_not_in_cohort",
            fields={"student": ["Not an active cohort member."]},
        )

    # Attachment keys come straight from the client (presigned earlier). Reject
    # any key not under this tenant's prefix so a student cannot persist
    # cross-tenant-shaped (or other-student) S3 references that a future
    # download / quota flow would inherit.
    keys = list(attachment_keys or [])
    prefix = f"{current_schema()}/assignments/"
    bad = [k for k in keys if not isinstance(k, str) or not k.startswith(prefix)]
    if bad:
        raise UnprocessableEntity(
            _("One or more attachment keys are not valid for this tenant."),
            code="invalid_attachment_key",
            fields={"attachment_keys": [f"Keys must start with '{prefix}'."]},
        )

    settings = get_center_settings()
    max_resubmits = (
        assignment.max_resubmits
        if assignment.max_resubmits is not None
        else settings.assignment_max_resubmits
    )
    last_attempt = (
        Submission.objects.filter(assignment=assignment, student=student)
        .order_by("-attempt_number")
        .values_list("attempt_number", flat=True)
        .first()
        or 0
    )
    attempt_number = last_attempt + 1
    if attempt_number > max_resubmits + 1:  # +1 = the original submission
        raise UnprocessableEntity(
            _("You have reached the resubmission limit for this assignment."),
            code="resubmit_limit_exceeded",
            fields={"attempt_number": [f"Limit is {max_resubmits + 1} attempt(s)."]},
        )

    grace = timedelta(minutes=settings.assignment_grace_minutes)
    is_late = timezone.now() > assignment.due_at + grace
    try:
        with transaction.atomic():
            submission = Submission.objects.create(
                assignment=assignment,
                student=student,
                text=text,
                attachments=keys,
                is_late=is_late,
                attempt_number=attempt_number,
            )
        # D4-A: a new submission requests AI feedback. Emitted on commit so the
        # receiver enqueues run_assignment_feedback exactly once per submission.
        schema = current_schema()
        actor_id = getattr(actor, "id", None)
        transaction.on_commit(
            lambda: ai_feedback_requested.send(
                sender=Submission,
                submission_id=submission.pk,
                requested_by=actor_id,
                schema_name=schema,
            )
        )
        return submission
    except IntegrityError as exc:
        # Two concurrent submits for the same (assignment, student) computed the
        # same attempt_number; the UniqueConstraint catches the loser. Surface a
        # clean 409 instead of a 500. (Nested atomic so the outer transaction
        # isn't poisoned by the broken savepoint.)
        raise ConflictException(
            _("A concurrent submission was detected. Please retry."),
            code="submission_conflict",
        ) from exc


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


@transaction.atomic
def grade_submission(*, submission: Submission, score, rubric_scores=None, feedback: str = "", actor=None):
    """Upsert a `SubmissionGrade`. Validates rubric criteria against the
    assignment's rubric and that Σ rubric max_points ≤ assignment.max_score."""
    assignment = submission.assignment
    score = Decimal(str(score))
    rubric_scores = list(rubric_scores or [])

    if score < 0 or score > assignment.max_score:
        raise UnprocessableEntity(
            _("Score is out of range."),
            code="score_out_of_range",
            fields={"score": [f"Must be between 0 and {assignment.max_score}."]},
        )

    valid_criteria = {row.get("criterion") for row in assignment.rubric}
    unknown = [rs.get("criterion") for rs in rubric_scores if rs.get("criterion") not in valid_criteria]
    if unknown:
        raise UnprocessableEntity(
            _("Rubric score references an unknown criterion."),
            code="unknown_rubric_criterion",
            fields={"rubric_scores": [f"Unknown criteria: {unknown}."]},
        )

    rubric_cap = sum(int(row.get("max_points", 0)) for row in assignment.rubric)
    if rubric_cap > assignment.max_score:
        raise UnprocessableEntity(
            _("The rubric's total points exceed the assignment's max score."),
            code="rubric_exceeds_max_score",
            fields={"rubric": [f"Σ max_points {rubric_cap} > max_score {assignment.max_score}."]},
        )

    grade, _created = SubmissionGrade.objects.update_or_create(
        submission=submission,
        defaults={
            "score": score,
            "rubric_scores": rubric_scores,
            "feedback": feedback,
            "graded_by": actor,
        },
    )
    submission.status = Submission.Status.GRADED
    submission.save(update_fields=["status"])

    schema = current_schema()
    transaction.on_commit(
        lambda: submission_graded.send(
            sender=Submission,
            submission_id=submission.pk,
            student_id=submission.student_id,
            score=str(score),
            schema_name=schema,
        )
    )
    return grade


# ---------------------------------------------------------------------------
# Plagiarism (D2-D-5 stub — interface only, no HTTP)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlagiarismResult:
    status: str
    score: float | None


def check_submission(submission: Submission) -> PlagiarismResult:
    """Plagiarism interface stub (real provider lands later). Never called from a
    request path; returns a typed not-implemented result."""
    return PlagiarismResult(status="not_implemented", score=None)


# ---------------------------------------------------------------------------
# AI feedback request (emit-only; D4-A consumes)
# ---------------------------------------------------------------------------


def request_ai_feedback(*, submission: Submission, requested_by=None) -> None:
    schema = current_schema()
    ai_feedback_requested.send(
        sender=Submission,
        submission_id=submission.pk,
        requested_by=getattr(requested_by, "id", None),
        schema_name=schema,
    )


# ---------------------------------------------------------------------------
# Beat task body (due-soon reminders)
# ---------------------------------------------------------------------------


def emit_due_soon_reminders() -> int:
    """Emit `assignment_due_soon` for published assignments due within 24h that
    haven't been reminded. `due_soon_sent_at` IS the idempotency key — a re-run
    skips them. Runs under the active tenant schema."""
    now = timezone.now()
    horizon = now + timedelta(hours=24)
    due = Assignment.objects.filter(
        status=Assignment.Status.PUBLISHED,
        due_soon_sent_at__isnull=True,
        due_at__gte=now,
        due_at__lte=horizon,
    )
    schema = current_schema()
    count = 0
    for assignment in due:
        assignment.due_soon_sent_at = now
        assignment.save(update_fields=["due_soon_sent_at"])
        assignment_due_soon.send(
            sender=Assignment,
            assignment_id=assignment.pk,
            cohort_id=assignment.cohort_id,
            due_at=assignment.due_at.isoformat(),
            schema_name=schema,
        )
        count += 1
    return count
