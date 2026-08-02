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
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.assignments.models import Assignment, AssignmentUploadGrant, Submission, SubmissionGrade
from apps.assignments.signals import (
    ai_feedback_requested,
    assignment_due_soon,
    assignment_published,
    submission_graded,
)
from apps.assignments.storage_keys import (
    final_attachment_key,
    parse_final_attachment_key,
    parse_legacy_attachment_key,
    parse_pending_attachment_key,
    pending_attachment_key,
)
from apps.cohorts.models import CohortMembership
from apps.org.selectors import get_center_settings
from core.attachment_storage import (
    AttachmentObjectError,
    allowed_attachment_mime_types,
    promote_attachment_object,
)
from core.exceptions import ConflictException, NotFoundException, UnprocessableEntity
from core.storage_keys import normalized_storage_filename
from core.utils import current_schema
from infrastructure.storage.s3_client import delete_object, presign_post_upload

# ---------------------------------------------------------------------------
# Attachment upload and record-bound promotion
# ---------------------------------------------------------------------------

_UPLOAD_GRANT_SECONDS = 600
_MAX_ATTACHMENTS = 20
_MAX_ACTIVE_UPLOAD_GRANTS = 40


@transaction.atomic
def validate_and_presign_upload(*, filename: str, content_type: str, size_bytes: int, requested_by) -> dict:
    """Issue one exact-size, owner-bound POST policy for a staging key."""

    settings = get_center_settings()
    normalized_filename = normalized_storage_filename(filename)
    if normalized_filename is None:
        raise UnprocessableEntity(
            _("That filename is not allowed."),
            code="invalid_filename",
            fields={"filename": ["Provide one safe filename of at most 255 UTF-8 bytes."]},
        )
    filename = normalized_filename
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
        raise UnprocessableEntity(
            _("The file size is invalid."),
            code="invalid_file_size",
            fields={"size_bytes": ["File size must be a positive integer."]},
        )
    content_type = content_type.partition(";")[0].strip().lower()
    if not content_type or len(content_type) > 127 or "/" not in content_type:
        raise UnprocessableEntity(
            _("The content type is invalid."),
            code="invalid_content_type",
            fields={"content_type": ["Provide a valid MIME content type."]},
        )
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed_extensions = {
        str(extension).lower().lstrip(".")
        for extension in settings.allowed_file_types
        if isinstance(extension, str)
    }
    if ext not in allowed_extensions:
        raise UnprocessableEntity(
            _("That file type is not allowed."),
            code="file_type_not_allowed",
            fields={"filename": [f"Extension '.{ext}' is not in the allowed list."]},
        )
    expected = allowed_attachment_mime_types(filename)
    if not expected:
        raise UnprocessableEntity(
            _("That file type is not supported for attachments."),
            code="file_type_not_allowed",
            fields={"filename": [f"Extension '.{ext}' has no reviewed attachment signature."]},
        )
    if content_type not in expected:
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

    requested_by.__class__.objects.select_for_update().get(pk=requested_by.pk)
    now = timezone.now()
    active_grants = AssignmentUploadGrant.objects.filter(
        requested_by=requested_by,
        consumed_at__isnull=True,
        expires_at__gt=now,
    ).count()
    if active_grants >= _MAX_ACTIVE_UPLOAD_GRANTS:
        raise UnprocessableEntity(
            _("Too many uploads are waiting to be attached."),
            code="upload_grant_limit",
            fields={"filename": ["Attach or let an earlier upload expire before requesting another."]},
        )

    expires_at = now + timedelta(seconds=_UPLOAD_GRANT_SECONDS)
    key = pending_attachment_key(
        schema=current_schema(),
        owner_id=requested_by.pk,
        upload_id=uuid.uuid4().hex,
        filename=filename,
    )
    grant = AssignmentUploadGrant.objects.create(
        key=key,
        requested_by=requested_by,
        content_type=content_type,
        expected_size_bytes=size_bytes,
        expires_at=expires_at,
    )
    post = presign_post_upload(
        key,
        content_type=content_type,
        size_bytes=size_bytes,
        expires_in=_UPLOAD_GRANT_SECONDS,
    )
    return {
        "url": post["url"],
        "fields": post["fields"],
        "method": "POST",
        "key": key,
        "grant_id": grant.pk,
        "expires_at": expires_at.isoformat(),
    }


def _normalize_attachment_keys(keys: object) -> list[str]:
    if not isinstance(keys, list) or len(keys) > _MAX_ATTACHMENTS:
        raise UnprocessableEntity(
            _("The attachment list is invalid."),
            code="invalid_attachment_key",
            fields={"attachments": [f"Provide a list of at most {_MAX_ATTACHMENTS} attachment keys."]},
        )
    if any(not isinstance(key, str) or not key or len(key) > 512 for key in keys):
        raise UnprocessableEntity(
            _("One or more attachment keys are malformed."),
            code="invalid_attachment_key",
            fields={"attachments": ["Every attachment key must be non-empty text."]},
        )
    if len(keys) != len(set(keys)):
        raise UnprocessableEntity(
            _("Duplicate attachment keys are not allowed."),
            code="invalid_attachment_key",
            fields={"attachments": ["Attachment keys must be unique."]},
        )
    return list(keys)


def _object_error(reason: str) -> UnprocessableEntity:
    if reason == "missing":
        return UnprocessableEntity(
            _("An uploaded attachment could not be found."),
            code="attachment_not_uploaded",
            fields={"attachments": ["Upload the file before attaching it."]},
        )
    if reason == "size":
        return UnprocessableEntity(
            _("An uploaded attachment has the wrong size."),
            code="attachment_size_mismatch",
            fields={"attachments": ["The stored size does not match its upload grant."]},
        )
    if reason == "content_type":
        return UnprocessableEntity(
            _("An uploaded attachment has the wrong content type."),
            code="attachment_type_mismatch",
            fields={"attachments": ["The stored type does not match its upload grant."]},
        )
    return UnprocessableEntity(
        _("An uploaded attachment does not contain the declared file type."),
        code="attachment_content_mismatch",
        fields={"attachments": ["Upload a file whose contents match its filename and type."]},
    )


def _locked_live_grants(*, keys: list[str], actor) -> dict[str, AssignmentUploadGrant]:
    if keys and actor is None:
        raise UnprocessableEntity(
            _("Attachment upload ownership is required."),
            code="invalid_attachment_grant",
            fields={"attachments": ["Request a new upload URL for this account."]},
        )
    schema = current_schema()
    parsed = {key: parse_pending_attachment_key(key, schema=schema) for key in keys}
    if any(value is None or value.owner_id != actor.pk for value in parsed.values()):
        raise UnprocessableEntity(
            _("One or more attachment keys are not authorized."),
            code="invalid_attachment_key",
            fields={"attachments": ["Use keys returned by your own assignment upload request."]},
        )
    now = timezone.now()
    grants = {
        grant.key: grant
        for grant in AssignmentUploadGrant.objects.select_for_update().filter(
            key__in=keys,
            requested_by=actor,
            consumed_at__isnull=True,
            expires_at__gt=now,
        )
    }
    if set(grants) != set(keys):
        raise UnprocessableEntity(
            _("An attachment upload grant is missing, expired, already used, or belongs to another user."),
            code="invalid_attachment_grant",
            fields={"attachments": ["Request a new upload URL and upload the file again."]},
        )
    return grants


def _target_identity(target: Assignment | Submission) -> tuple[str, int]:
    if not target.pk:
        raise ValueError("An attachment target must be saved before promotion")
    if isinstance(target, Assignment):
        return "assignments", target.pk
    if isinstance(target, Submission):
        return "submissions", target.pk
    raise TypeError("Unsupported assignment attachment target")


def _trusted_new_final_keys(target: Assignment | Submission, keys: list[str]) -> set[str]:
    """Return only canonical keys backed by a consumed server upload grant."""

    schema = current_schema()
    target_kind, target_id = _target_identity(target)
    parsed = {key: parse_final_attachment_key(key, schema=schema) for key in keys}
    grant_ids = {
        item.grant_id
        for item in parsed.values()
        if item is not None and item.target_kind == target_kind and item.target_id == target_id
    }
    grants = {
        grant.pk: grant
        for grant in AssignmentUploadGrant.objects.filter(
            pk__in=grant_ids,
            consumed_at__isnull=False,
            durable_deleted_at__isnull=True,
        )
    }
    trusted: set[str] = set()
    for key, item in parsed.items():
        if item is None or item.target_kind != target_kind or item.target_id != target_id:
            continue
        grant = grants.get(item.grant_id)
        if grant is None:
            continue
        source = parse_pending_attachment_key(grant.key, schema=schema)
        if source is not None and source.filename == item.filename and grant.durable_key in (None, key):
            expected_key = final_attachment_key(
                schema=schema,
                target_kind=target_kind,
                target_id=target_id,
                grant_id=grant.pk,
                filename=source.filename,
            )
            if key == expected_key:
                trusted.add(key)
    return trusted


def _legacy_key_is_bound(target: Assignment | Submission, key: str) -> bool:
    """Safely retain deployed pre-promotion attachments when uniquely attributable."""

    schema = current_schema()
    if parse_legacy_attachment_key(key, schema=schema) is None:
        return False
    grants = AssignmentUploadGrant.objects.filter(
        Q(durable_key=key) | Q(durable_key__isnull=True),
        key=key,
        consumed_at__isnull=False,
        durable_deleted_at__isnull=True,
    )
    if isinstance(target, Submission):
        grants = grants.filter(requested_by_id=target.student.user_id)
    if not grants.exists():
        return False
    assignment_ids = list(
        Assignment.objects.filter(attachments__contains=[key]).values_list("pk", flat=True)[:2]
    )
    submission_ids = list(
        Submission.objects.filter(attachments__contains=[key]).values_list("pk", flat=True)[:2]
    )
    if len(assignment_ids) + len(submission_ids) != 1:
        return False
    if isinstance(target, Assignment):
        return assignment_ids == [target.pk]
    return submission_ids == [target.pk]


def trusted_attachment_keys(target: Assignment | Submission) -> tuple[str, ...]:
    """Resolve storage references that are provably bound to this exact row."""

    raw = target.attachments if isinstance(target.attachments, list) else []
    string_keys = [key for key in raw if isinstance(key, str)]
    trusted_final = _trusted_new_final_keys(target, string_keys)
    return tuple(key for key in string_keys if key in trusted_final or _legacy_key_is_bound(target, key))


def _delete_promoted_objects(keys: list[str]) -> None:
    """Best-effort compensation before a failed database transaction unwinds."""

    schema = current_schema()
    for key in keys:
        if parse_final_attachment_key(key, schema=schema) is None:
            continue
        with suppress(Exception):
            delete_object(key)


def discard_promoted_attachment_keys(keys: list[str]) -> None:
    """Compensate storage promotion when its enclosing database write fails."""

    _delete_promoted_objects(keys)


def _enqueue_source_cleanup(grant_ids: list[int]) -> None:
    if not grant_ids:
        return
    schema = current_schema()

    def enqueue() -> None:
        from celery_tasks.attachment_tasks import cleanup_consumed_upload_sources_for_schema

        cleanup_consumed_upload_sources_for_schema.delay("assignments", grant_ids, _schema_name=schema)

    transaction.on_commit(enqueue, robust=True)


def enqueue_attachment_deletions(keys: list[str]) -> None:
    if not keys:
        return
    schema = current_schema()
    unique_keys = list(dict.fromkeys(keys))
    key_chunks = [unique_keys[index : index + 500] for index in range(0, len(unique_keys), 500)]
    now = timezone.now()
    grant_ids: list[int] = []
    for chunk in key_chunks:
        grants = AssignmentUploadGrant.objects.filter(
            consumed_at__isnull=False,
            durable_deleted_at__isnull=True,
        ).filter(Q(durable_key__in=chunk) | Q(durable_key__isnull=True, key__in=chunk))
        chunk_ids = list(grants.values_list("pk", flat=True))
        if chunk_ids:
            AssignmentUploadGrant.objects.filter(pk__in=chunk_ids).update(deletion_requested_at=now)
            grant_ids.extend(chunk_ids)
    grant_ids = list(dict.fromkeys(grant_ids))
    grant_chunks = [grant_ids[index : index + 500] for index in range(0, len(grant_ids), 500)]

    def enqueue() -> None:
        from celery_tasks.attachment_tasks import delete_attachment_objects

        for chunk in grant_chunks:
            delete_attachment_objects.delay("assignments", chunk, _schema_name=schema)

    transaction.on_commit(enqueue, robust=True)


def consume_assignment_attachments(*, target: Assignment | Submission, keys: list, actor) -> list[str]:
    """Promote new staging keys and retain only trusted keys already on ``target``."""

    normalized = _normalize_attachment_keys(keys)
    if not normalized:
        enqueue_attachment_deletions(list(trusted_attachment_keys(target)))
        return []

    current = set(trusted_attachment_keys(target))
    pending_keys = [key for key in normalized if key not in current]
    if any(parse_pending_attachment_key(key, schema=current_schema()) is None for key in pending_keys):
        raise UnprocessableEntity(
            _("One or more attachment keys are not authorized for this record."),
            code="invalid_attachment_key",
            fields={"attachments": ["Use an existing attachment or a new upload grant."]},
        )
    grants = _locked_live_grants(keys=pending_keys, actor=actor)
    target_kind, target_id = _target_identity(target)
    promoted_by_source: dict[str, str] = {}
    promoted: list[str] = []
    now = timezone.now()
    try:
        for source_key in pending_keys:
            grant = grants[source_key]
            parsed_source = parse_pending_attachment_key(source_key, schema=current_schema())
            if parsed_source is None:  # guarded above; keeps type narrowing explicit
                raise UnprocessableEntity(code="invalid_attachment_key")
            destination_key = final_attachment_key(
                schema=current_schema(),
                target_kind=target_kind,
                target_id=target_id,
                grant_id=grant.pk,
                filename=parsed_source.filename,
            )
            try:
                verified = promote_attachment_object(
                    source_key=source_key,
                    destination_key=destination_key,
                    filename=parsed_source.filename,
                    expected_size_bytes=grant.expected_size_bytes,
                    expected_content_type=grant.content_type,
                )
            except AttachmentObjectError as exc:
                raise _object_error(exc.reason) from exc
            promoted.append(destination_key)
            grant.actual_size_bytes = verified.size_bytes
            grant.consumed_at = now
            grant.durable_key = destination_key
            grant.save(update_fields=["actual_size_bytes", "consumed_at", "durable_key"])
            promoted_by_source[source_key] = destination_key
    except Exception:
        _delete_promoted_objects(promoted)
        raise

    _enqueue_source_cleanup([grants[key].pk for key in pending_keys])
    result = [promoted_by_source.get(key, key) for key in normalized]
    removed = [key for key in current if key not in result]
    enqueue_attachment_deletions(removed)
    return result


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
def close_assignment(*, assignment: Assignment, actor=None) -> Assignment:
    """Close an open assignment; retries are idempotent and drafts cannot skip publish."""
    assignment = Assignment.objects.select_for_update().get(pk=assignment.pk)
    if assignment.status == Assignment.Status.CLOSED:
        return assignment
    if assignment.status != Assignment.Status.PUBLISHED:
        raise UnprocessableEntity(
            _("Only a published assignment can be closed."),
            code="assignment_not_published",
            fields={"status": [f"Cannot close from status '{assignment.status}'."]},
        )
    assignment.status = Assignment.Status.CLOSED
    assignment.save(update_fields=["status", "updated_at"])
    return assignment


@transaction.atomic
def submit(
    *, assignment: Assignment, student, text: str = "", attachment_keys=None, actor=None
) -> Submission:
    """Create a submission. Rejects draft/closed assignments, non-members, and
    attempts past the resubmit limit — each with its own 422 code."""
    # Serialize against publish/close/delete/cohort moves and re-read the state
    # after taking the lock. A stale view-level object must not accept an upload
    # into an assignment that closed or moved out of the student's cohort.
    locked_assignment = (
        Assignment.objects.select_for_update().select_related("cohort").filter(pk=assignment.pk).first()
    )
    if locked_assignment is None:
        raise NotFoundException(_("Assignment not found."), code="not_found")
    assignment = locked_assignment
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
    now = timezone.now()
    # Compare elapsed time instead of adding to due_at; datetime.max + grace
    # overflows even though such a far-future submission is plainly not late.
    is_late = now > assignment.due_at and (now - assignment.due_at) > grace
    try:
        with transaction.atomic():
            submission = Submission.objects.create(
                assignment=assignment,
                student=student,
                text=text,
                # Durable object keys include the submission primary key.
                attachments=[],
                is_late=is_late,
                attempt_number=attempt_number,
            )
    except IntegrityError as exc:
        # Two concurrent submits for the same (assignment, student) computed the
        # same attempt_number; the UniqueConstraint catches the loser. Surface a
        # clean 409 instead of a 500. (Nested atomic so the outer transaction
        # isn't poisoned by the broken savepoint.)
        raise ConflictException(
            _("A concurrent submission was detected. Please retry."),
            code="submission_conflict",
        ) from exc

    promoted: list[str] = []
    try:
        promoted = consume_assignment_attachments(
            target=submission,
            keys=list(attachment_keys or []),
            actor=actor,
        )
        submission.attachments = promoted
        submission.save(update_fields=["attachments"])
    except Exception:
        _delete_promoted_objects(promoted)
        raise
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


@transaction.atomic
def return_submission(*, submission: Submission, actor=None) -> Submission:
    """Return a submission for revision; a repeated teacher action is a no-op."""
    submission = Submission.objects.select_for_update().get(pk=submission.pk)
    if submission.status == Submission.Status.RETURNED:
        return submission
    submission.status = Submission.Status.RETURNED
    submission.save(update_fields=["status"])
    return submission


# ---------------------------------------------------------------------------
# Plagiarism (D2-D-5 local tenant-scoped detector)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlagiarismResult:
    status: str
    score: float | None
    matched_submission_id: int | None = None


def check_submission(submission: Submission) -> PlagiarismResult:
    """Compare one response with other students' responses for the assignment.

    This is a deterministic local baseline rather than a fictional external
    provider. Five-word shingles tolerate punctuation/case changes, and every
    candidate remains inside the current tenant and assignment.
    """
    import re

    def shingles(text: str) -> set[tuple[str, ...]]:
        words = re.findall(r"\w+", (text or "").casefold(), flags=re.UNICODE)
        if len(words) < 5:
            return set()
        return {tuple(words[index : index + 5]) for index in range(len(words) - 4)}

    source = shingles(submission.text)
    if not source:
        return PlagiarismResult(status="insufficient_text", score=None)

    best_score = 0.0
    matched_submission_id: int | None = None
    candidates = (
        Submission.objects.filter(assignment_id=submission.assignment_id)
        .exclude(pk=submission.pk)
        .exclude(student_id=submission.student_id)
        .only("id", "text")
    )
    for candidate in candidates.iterator(chunk_size=200):
        candidate_shingles = shingles(candidate.text)
        if not candidate_shingles:
            continue
        score = len(source & candidate_shingles) / len(source | candidate_shingles)
        if score > best_score:
            best_score = score
            matched_submission_id = candidate.pk
            if score == 1.0:
                break
    return PlagiarismResult(
        status="completed",
        score=round(best_score, 4),
        matched_submission_id=matched_submission_id,
    )


# ---------------------------------------------------------------------------
# AI feedback request (emit-only; D4-A consumes)
# ---------------------------------------------------------------------------


def request_ai_feedback(*, submission: Submission, requested_by=None, requested_principal=None) -> None:
    schema = current_schema()
    ai_feedback_requested.send(
        sender=Submission,
        submission_id=submission.pk,
        requested_by=getattr(requested_by, "id", None),
        requested_principal_kind=getattr(requested_principal, "kind", None),
        requested_principal_id=getattr(requested_principal, "principal_id", None),
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
