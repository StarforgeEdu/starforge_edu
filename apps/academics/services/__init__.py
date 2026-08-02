"""Academics write services (TASKS §11, TD-13/14).

Exam results upsert with a `grade_changed` audit signal on overwrite; CSV import
is all-or-nothing; `compute_term_grade` rolls published results into a weighted
0-100 `Grade` rendered per the Center's scheme; transcripts are generated
off-request (weasyprint → S3) by `generate_transcript`. No notification dispatch
happens here (D3-D consumes `grade_changed`); bulk writes explicitly preserve the
normal model audit trail.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.academics.dto import ResultFieldError, validate_result_values
from apps.academics.integrity import (
    ExamReadiness,
    assessment_integrity_write,
    recompute_term_grades,
)
from apps.academics.integrity import (
    correct_exam as correct_exam_transition,
)
from apps.academics.integrity import (
    exam_readiness as assessment_readiness,
)
from apps.academics.integrity import (
    publish_exam as publish_exam_transition,
)
from apps.academics.models import Exam, ExamResult, Grade, Transcript
from apps.cohorts.models import CohortMembership
from apps.students.models import StudentProfile
from core.exceptions import ConflictException, UnprocessableEntity, ValidationException
from core.utils import current_schema
from infrastructure.storage.s3_client import presign_download, upload_bytes

# Max rows accepted in one results-CSV import (bounds memory + per-row DB work).
MAX_IMPORT_ROWS = 5000
# 5,000 normal rows, including bounded notes, fit comfortably while a tenant's
# much broader generic document-upload ceiling no longer controls this parser.
MAX_IMPORT_BYTES = 2 * 1024 * 1024

logger = logging.getLogger("starforge.academics")


# ---------------------------------------------------------------------------
# Exam results
# ---------------------------------------------------------------------------


@transaction.atomic
def record_results(*, exam: Exam, rows: list[dict], actor=None) -> dict:
    """Upsert `[{student, score, note?}]` for `exam`. Scores outside
    `0..max_score` abort the whole batch with **422**. Overwriting an existing
    result with a DIFFERENT score emits `grade_changed` exactly once (never on
    first entry, and never when the score is unchanged)."""
    if not rows:
        return {"created": 0, "updated": 0, "results": []}

    # Serialize batches for one exam so the existing-result snapshot is stable
    # and concurrent imports cannot race on (exam, student).
    exam = Exam.objects.select_related("cohort").select_for_update().get(pk=exam.pk)
    if exam.is_published or exam.requires_republish:
        raise ConflictException(
            _("Published results can only change through an explicit correction."),
            code="exam_results_locked",
        )

    duplicate_errors: dict[str, list[str]] = {}
    field_errors: dict[str, list[str]] = {}
    seen_student_ids: set[int] = set()
    for index, row in enumerate(rows):
        student_id = row["student"].pk
        if student_id in seen_student_ids:
            duplicate_errors[str(index)] = [str(_("A student may appear only once per batch."))]
        seen_student_ids.add(student_id)
        try:
            values = validate_result_values(
                score=row.get("score"),
                note=row.get("note", ""),
                max_score=exam.max_score,
            )
        except ResultFieldError as exc:
            field_errors.setdefault(str(index), []).append(exc.message)
        else:
            row["score"] = values.score
            row["note"] = values.note
    if duplicate_errors:
        raise UnprocessableEntity(
            _("A student may appear only once per batch."),
            code="duplicate_student",
            fields=duplicate_errors,
        )
    if field_errors:
        raise UnprocessableEntity(
            _("One or more scores are out of range."), code="score_out_of_range", fields=field_errors
        )

    # Every result row must be for a student actively enrolled in THIS exam's
    # cohort. ResultEntrySerializer.student is unscoped (StudentProfile.all()), so
    # without this a teacher reaching a scoped exam could still record/overwrite a
    # grade for any tenant student outside the cohort they teach.
    valid_student_ids = set(
        CohortMembership.objects.filter(cohort=exam.cohort, end_date__isnull=True).values_list(
            "student_id", flat=True
        )
    )
    member_errors = {
        str(index): [_("Student is not enrolled in this exam's cohort.")]
        for index, row in enumerate(rows)
        if row["student"].pk not in valid_student_ids
    }
    if member_errors:
        raise UnprocessableEntity(
            _("One or more students are not in this exam's cohort."),
            code="student_not_in_cohort",
            fields=member_errors,
        )

    existing_by_student = {
        result.student_id: result
        for result in ExamResult.objects.select_for_update().filter(
            exam=exam, student_id__in=seen_student_ids
        )
    }
    from apps.audit.context import current_request
    from apps.audit.models import AuditLog
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import (
        audit_logs_bulk_on_commit,
        diff_snapshots,
        serialize_instance,
    )

    before_snapshots = {
        student_id: serialize_instance(result) for student_id, result in existing_by_student.items()
    }

    now = timezone.now()
    audit_scope = scoped_audit_scope(exam.cohort.branch_id, exam.cohort.department_id)
    to_create: list[ExamResult] = []
    to_update: list[ExamResult] = []
    ordered_results: list[ExamResult] = []
    for row in rows:
        student = row["student"]
        result = existing_by_student.get(student.pk)
        if result is None:
            result = ExamResult(
                exam=exam,
                student=student,
                score=row["score"],
                note=row.get("note", ""),
                graded_by=actor,
                graded_at=now,
            )
            to_create.append(result)
        else:
            if result.score == row["score"] and result.note == row.get("note", ""):
                ordered_results.append(result)
                continue
            result.score = row["score"]
            result.note = row.get("note", "")
            result.graded_by = actor
            result.graded_at = now
            to_update.append(result)
        ordered_results.append(result)
    with assessment_integrity_write():
        if to_create:
            ExamResult.objects.bulk_create(to_create)
        if to_update:
            ExamResult.objects.bulk_update(
                to_update,
                fields=("score", "note", "graded_by", "graded_at"),
            )
    request = current_request()
    audit_entries = [
        {
            "actor": actor,
            "action": AuditLog.Action.CREATE,
            "resource_type": "academics.ExamResult",
            "resource_id": result.pk,
            "after": serialize_instance(result),
            "request": request,
            "scope": audit_scope,
        }
        for result in to_create
    ]
    audit_entries.extend(
        {
            "actor": actor,
            "action": AuditLog.Action.UPDATE,
            "resource_type": "academics.ExamResult",
            "resource_id": result.pk,
            "before": before_snapshots[result.student_id],
            "after": diff_snapshots(
                before_snapshots[result.student_id],
                serialize_instance(result),
            ),
            "request": request,
            "scope": audit_scope,
        }
        for result in to_update
    )
    audit_logs_bulk_on_commit(audit_entries)
    # Draft changes never emit the grades-published signal. Refresh any existing
    # aggregate from the currently published evidence so stale rows cannot
    # survive, but preserve their publication state when the inputs are unchanged.
    recompute_term_grades(
        student_ids=seen_student_ids,
        subject_id=exam.subject_id,
        term_id=exam.term_id,
        publish=None,
    )
    return {
        "created": len(to_create),
        "updated": len(to_update),
        "results": ordered_results,
    }


@transaction.atomic
def bulk_grade_import(*, exam: Exam, csv_file, actor=None) -> dict:
    """Parse a `student_id,score,note?` CSV and record every row, or **422** with
    per-row errors and **zero rows written** if any row is invalid (DoD)."""
    # Bound input: file size (mirrors import_students_csv) + a row cap, so an
    # unbounded CSV can't exhaust memory / per-row DB work.
    max_bytes = MAX_IMPORT_BYTES
    size = getattr(csv_file, "size", None)
    if size is not None and size > max_bytes:
        raise ValidationException(
            _("CSV files may not exceed %(bytes)s bytes.") % {"bytes": max_bytes},
            code="file_too_large",
        )
    raw = csv_file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValidationException(
            _("CSV files may not exceed %(bytes)s bytes.") % {"bytes": max_bytes},
            code="file_too_large",
        )
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            # A Latin-1 / Windows-1252 export (Excel's default) would otherwise raise
            # an uncaught UnicodeDecodeError -> hard 500; surface a clean 400 instead.
            raise ValidationException(_("CSV file must be UTF-8 encoded."), code="bad_encoding") from None
    else:
        text = raw
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    if not {"student_id", "score"} <= fieldnames or fieldnames - {"student_id", "score", "note"}:
        raise ValidationException(
            _("CSV columns must be student_id, score, and optional note."),
            code="bad_csv_header",
        )

    # Restrict CSV rows to students actively enrolled in this exam's cohort, so a
    # global student_id lookup can't write grades for students outside the cohort.
    valid_student_ids = set(
        CohortMembership.objects.filter(cohort=exam.cohort, end_date__isnull=True).values_list(
            "student_id", flat=True
        )
    )
    raw_rows: list[tuple[int, str, dict]] = []
    for line_no, raw_row in enumerate(reader, start=2):  # line 1 is the header
        if line_no - 1 > MAX_IMPORT_ROWS:
            raise ValidationException(
                _("CSV exceeds the maximum of %(n)s rows.") % {"n": MAX_IMPORT_ROWS},
                code="too_many_rows",
            )
        raw_rows.append((line_no, (raw_row.get("student_id") or "").strip(), raw_row))

    students_by_code = StudentProfile.objects.in_bulk(
        {code for _line_no, code, _raw_row in raw_rows if code},
        field_name="student_id",
    )
    rows: list[dict] = []
    row_errors: list[dict] = []
    seen_codes: set[str] = set()
    for line_no, code, raw_row in raw_rows:
        if code in seen_codes:
            row_errors.append({"row": line_no, "error": f"Duplicate student_id '{code}'."})
            continue
        seen_codes.add(code)
        student = students_by_code.get(code)
        if student is None:
            row_errors.append({"row": line_no, "error": f"Unknown student_id '{code}'."})
            continue
        if student.pk not in valid_student_ids:
            row_errors.append({"row": line_no, "error": f"Student '{code}' is not in this exam's cohort."})
            continue
        try:
            values = validate_result_values(
                score=(raw_row.get("score") or "").strip(),
                note=raw_row.get("note") or "",
                max_score=exam.max_score,
            )
        except ResultFieldError as exc:
            row_errors.append({"row": line_no, "field": exc.field, "error": exc.message})
            continue
        rows.append({"student": student, "score": values.score, "note": values.note})

    if row_errors:
        raise UnprocessableEntity(
            _("CSV has invalid rows; nothing was imported."),
            code="csv_row_errors",
            fields={"rows": row_errors},
        )
    return record_results(exam=exam, rows=rows, actor=actor)


def publish_exam(
    *,
    exam: Exam,
    actor=None,
    expected_version: int,
    confirmed: bool,
) -> tuple[Exam, ExamReadiness]:
    return publish_exam_transition(
        exam=exam,
        actor=actor,
        expected_version=expected_version,
        confirmed=confirmed,
    )


def exam_readiness(*, exam: Exam):
    return assessment_readiness(exam=exam)


def correct_exam(
    *,
    exam: Exam,
    changes: dict,
    rows: list[dict],
    reason: str,
    expected_version: int,
    actor=None,
):
    return correct_exam_transition(
        exam=exam,
        changes=changes,
        rows=rows,
        reason=reason,
        expected_version=expected_version,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# Term grades
# ---------------------------------------------------------------------------


def compute_term_grade(*, student, subject, term, settings=None, publish: bool = False) -> Grade | None:
    """Weighted 0-100 term grade from **published** exam results:
    `100 * sum(score/max * weight) / sum(weight)`. Returns None when nothing
    published contributes. Writes/updates the `Grade` with a `components`
    breakdown and a scheme-rendered `value_display`."""
    # ``settings`` remains accepted for call compatibility; the canonical
    # recompute path loads the tenant settings once for the whole batch.
    del settings
    grades = recompute_term_grades(
        student_ids=[student.pk],
        subject_id=subject.pk,
        term_id=term.pk,
        publish=True if publish else None,
    )
    return grades[0] if grades else None


def recompute_cohort_term(*, cohort, subject, term, publish: bool = False) -> list[Grade]:
    """Recompute every active member's grade for (subject, term)."""
    student_ids = CohortMembership.objects.filter(
        cohort=cohort,
        end_date__isnull=True,
    ).values_list("student_id", flat=True)
    return recompute_term_grades(
        student_ids=student_ids,
        subject_id=subject.pk,
        term_id=term.pk,
        publish=True if publish else None,
    )


# ---------------------------------------------------------------------------
# Transcripts (TD-14: weasyprint → S3, off-request)
# ---------------------------------------------------------------------------


@transaction.atomic
def request_transcript(*, student, term=None, requested_by=None) -> Transcript:
    """Idempotently admit a transcript under strict user/tenant queue caps."""
    from django.conf import settings

    from core.exceptions import ThrottledException
    from core.job_limits import lock_tenant_job_queue

    lock_tenant_job_queue("documents")
    active = (Transcript.Status.PENDING, Transcript.Status.PROCESSING)
    duplicate = (
        Transcript.objects.filter(
            student=student,
            term=term,
            requested_by=requested_by,
            status__in=active,
        )
        .order_by("-created_at")
        .first()
    )
    if duplicate is not None:
        return duplicate

    now = timezone.now()
    user_active = Transcript.objects.filter(requested_by=requested_by, status__in=active).count()
    tenant_active = Transcript.objects.filter(status__in=active).count()
    user_hourly = Transcript.objects.filter(
        requested_by=requested_by, created_at__gte=now - timedelta(hours=1)
    ).count()
    tenant_hourly = Transcript.objects.filter(created_at__gte=now - timedelta(hours=1)).count()
    from apps.reports.models import ReportRun

    report_active = ReportRun.objects.filter(
        status__in=(ReportRun.Status.QUEUED, ReportRun.Status.RUNNING)
    ).count()
    report_hourly = ReportRun.objects.filter(created_at__gte=now - timedelta(hours=1)).count()
    if user_active >= getattr(settings, "TRANSCRIPT_MAX_ACTIVE_PER_USER", 3):
        raise ThrottledException(code="transcript_user_queue_full", wait=60)
    if tenant_active >= getattr(settings, "TRANSCRIPT_MAX_ACTIVE_PER_TENANT", 20):
        raise ThrottledException(code="transcript_tenant_queue_full", wait=60)
    if tenant_active + report_active >= getattr(settings, "DOCUMENT_MAX_ACTIVE_PER_TENANT", 20):
        raise ThrottledException(code="document_tenant_queue_full", wait=60)
    if user_hourly >= getattr(settings, "TRANSCRIPT_MAX_HOURLY_PER_USER", 10):
        raise ThrottledException(code="transcript_user_hourly_limit", wait=3600)
    if tenant_hourly >= getattr(settings, "TRANSCRIPT_MAX_HOURLY_PER_TENANT", 100):
        raise ThrottledException(code="transcript_tenant_hourly_limit", wait=3600)
    if tenant_hourly + report_hourly >= getattr(settings, "DOCUMENT_MAX_HOURLY_PER_TENANT", 100):
        raise ThrottledException(code="document_tenant_hourly_limit", wait=3600)

    transcript = Transcript.objects.create(student=student, term=term, requested_by=requested_by)
    schema = current_schema()
    transaction.on_commit(lambda: _enqueue_transcript(transcript.pk, schema))
    return transcript


def _enqueue_transcript(transcript_id: int, schema: str) -> None:
    from celery_tasks.academics_tasks import generate_transcript_pdf

    generate_transcript_pdf.delay(transcript_id, _schema_name=schema)


def render_transcript_pdf(transcript: Transcript) -> bytes:
    """Render the transcript HTML to PDF bytes. weasyprint is imported lazily so
    the app loads where its GTK native libs are absent (e.g. a Windows dev box);
    only this call needs them."""
    from django.template.loader import render_to_string
    from django.utils import translation
    from weasyprint import HTML  # lazy on purpose: GTK native libs only needed here

    student = transcript.student
    lang = getattr(student.user, "preferred_language", "en")
    grades = (
        Grade.objects.filter(student=student, is_published=True)
        .select_related("subject", "term")
        .order_by("subject__name")
    )
    if transcript.term_id:
        grades = grades.filter(term_id=transcript.term_id)
    with translation.override(lang):
        html = render_to_string(
            "documents/transcript.html",
            {"transcript": transcript, "student": student, "grades": grades},
        )
    return HTML(string=html).write_pdf()


def generate_transcript(transcript_id: int) -> str:
    from core.exceptions import ConflictException
    from core.job_limits import release_job_execution, try_acquire_job_execution

    if not try_acquire_job_execution("transcript", transcript_id):
        raise ConflictException(
            _("This transcript is already being generated."), code="transcript_in_progress"
        )
    try:
        return _generate_transcript(transcript_id)
    finally:
        release_job_execution("transcript", transcript_id)


def _generate_transcript(transcript_id: int) -> str:
    """Idempotent task body: pending → processing → done, uploading the PDF to
    `{schema}/transcripts/{id}.pdf`. A `done` transcript short-circuits (re-run
    safe). Runs under the active tenant schema."""
    transcript = Transcript.objects.select_related("student__user", "term").get(pk=transcript_id)
    if transcript.status == Transcript.Status.DONE:
        return transcript.pdf_key

    transcript.status = Transcript.Status.PROCESSING
    transcript.save(update_fields=["status"])

    pdf = render_transcript_pdf(transcript)
    key = f"{current_schema()}/transcripts/{transcript.pk}.pdf"
    upload_bytes(key, pdf, content_type="application/pdf")

    transcript.pdf_key = key
    transcript.status = Transcript.Status.DONE
    transcript.generated_at = timezone.now()
    transcript.save(update_fields=["pdf_key", "status", "generated_at"])
    return key


def mark_transcript_failed(transcript_id: int, exc: Exception) -> None:
    logger.error(
        "Transcript %s generation failed",
        transcript_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    Transcript.objects.filter(pk=transcript_id).update(
        status=Transcript.Status.FAILED,
        error="transcript_generation_failed",
    )


def presign_transcript(transcript: Transcript) -> str | None:
    if transcript.status == Transcript.Status.DONE and transcript.pdf_key:
        return presign_download(transcript.pdf_key, expires_in=600)
    return None
