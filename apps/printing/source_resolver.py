"""Resolve printable documents without trusting client-supplied storage keys.

The object-store key on a :class:`~apps.printing.models.PrintJob` is a capability:
whoever can make the branch agent claim the job receives a short-lived download
URL for that object.  The HTTP API therefore identifies a domain object and this
module derives the only key and branch that object is allowed to use.

The low-level ``enqueue_print`` service keeps its existing key-based signature for
trusted internal producers.  Agent claims re-check the same source/key/branch
relationship so a legacy or incorrectly-created row is never turned into a signed
download URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Never

from django.utils.translation import gettext_lazy as _

from apps.printing.models import PrintJob
from core.exceptions import NotFoundException, UnprocessableEntity, ValidationException
from core.permissions import get_user_roles
from core.scoping import request_permission_membership_allows, scope_to_permission_memberships
from core.utils import current_schema

SOURCE_READ_PERMISSIONS: dict[str, str] = {
    PrintJob.Source.ASSIGNMENT: "assignments:read",
    PrintJob.Source.TRANSCRIPT: "academics:read",
    PrintJob.Source.REPORT: "reports:read",
    PrintJob.Source.RECEIPT: "finance:read",
    PrintJob.Source.CONTENT: "content:read",
    PrintJob.Source.UPLOAD: "printing:write",
}

_PRINTABLE_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}


@dataclass(frozen=True, slots=True)
class ResolvedPrintSource:
    """Server-owned fields used to enqueue one authorized print job."""

    source: str
    source_id: int
    payload_s3_key: str
    branch_id: int | None
    cohort_id: int | None = None
    content_type: str | None = None
    size_bytes: int | None = None


def source_read_permission(source: str) -> str:
    try:
        return SOURCE_READ_PERMISSIONS[source]
    except KeyError as exc:
        raise ValidationException(
            _("Unknown print source."),
            code="invalid_source",
            fields={"source": ["Choose a supported print source."]},
        ) from exc


def resolve_print_source(
    *,
    request: Any,
    source: str,
    source_id: int,
    attachment_index: int | None = None,
) -> ResolvedPrintSource:
    """Load an in-scope source and derive its exact object key and branch.

    Missing and out-of-scope identifiers deliberately share the same 404.  A source
    that exists but has no completed server-owned file returns a stable 422 instead
    of accepting a guessed key.
    """

    if source == PrintJob.Source.ASSIGNMENT:
        return _resolve_assignment(
            request=request,
            source_id=source_id,
            attachment_index=attachment_index,
        )
    if attachment_index is not None:
        raise ValidationException(
            _("attachment_index is only valid for assignment sources."),
            code="validation_error",
            fields={"attachment_index": ["Remove this field for the selected source."]},
        )
    if source == PrintJob.Source.TRANSCRIPT:
        return _resolve_transcript(request=request, source_id=source_id)
    if source == PrintJob.Source.REPORT:
        return _resolve_report(request=request, source_id=source_id)
    if source == PrintJob.Source.RECEIPT:
        return _resolve_receipt(request=request, source_id=source_id)
    if source == PrintJob.Source.CONTENT:
        return _resolve_content_file(request=request, source_id=source_id)
    if source == PrintJob.Source.UPLOAD:
        raise ValidationException(
            _("Uploaded print files must use an owned upload grant."),
            code="invalid_print_upload",
            fields={"source_id": ["Use the grant id returned by the print upload endpoint."]},
        )
    source_read_permission(source)  # raises the canonical invalid-source response
    raise AssertionError("unreachable")  # pragma: no cover


def _resolve_assignment(*, request: Any, source_id: int, attachment_index: int | None) -> ResolvedPrintSource:
    from apps.assignments.selectors import scoped_assignments
    from apps.assignments.services import trusted_attachment_keys

    assignment = (
        scoped_assignments(user=request.user, roles=get_user_roles(request))
        .select_related("cohort")
        .filter(pk=source_id)
        .first()
    )
    if assignment is None:
        _not_found()

    attachments = assignment.attachments
    if not isinstance(attachments, list) or not attachments:
        _not_ready("The assignment has no printable attachment.")
    if attachment_index is None:
        if len(attachments) != 1:
            raise ValidationException(
                _("Choose the assignment attachment to print."),
                code="attachment_index_required",
                fields={"attachment_index": ["Required when an assignment has multiple attachments."]},
            )
        attachment_index = 0
    if attachment_index < 0 or attachment_index >= len(attachments):
        raise ValidationException(
            _("The selected attachment does not exist."),
            code="validation_error",
            fields={"attachment_index": ["Choose an existing assignment attachment."]},
        )

    key = attachments[attachment_index]
    # A tenant-prefixed string is not sufficient authority: a poisoned legacy
    # row could point at another assignment's otherwise valid object. Require
    # the consumed upload grant and canonical record-bound key proven by the
    # assignment storage service before creating a durable print capability.
    if key not in trusted_attachment_keys(assignment) or not _is_assignment_key(key):
        _not_ready("The assignment attachment has an invalid storage reference.")
    return ResolvedPrintSource(
        source=PrintJob.Source.ASSIGNMENT,
        source_id=assignment.pk,
        payload_s3_key=key,
        branch_id=assignment.cohort.branch_id,
        cohort_id=assignment.cohort_id,
    )


def _resolve_transcript(*, request: Any, source_id: int) -> ResolvedPrintSource:
    from apps.academics.models import Transcript
    from apps.academics.selectors import scoped_transcripts

    transcript = (
        scoped_transcripts(user=request.user, roles=get_user_roles(request))
        .select_related("student")
        .filter(pk=source_id)
        .first()
    )
    if transcript is None:
        _not_found()
    expected_key = _transcript_key(transcript.pk)
    if transcript.status != Transcript.Status.DONE or transcript.pdf_key != expected_key:
        _not_ready("The transcript file is not ready.")
    return ResolvedPrintSource(
        source=PrintJob.Source.TRANSCRIPT,
        source_id=transcript.pk,
        payload_s3_key=expected_key,
        branch_id=transcript.student.branch_id,
        cohort_id=transcript.student.current_cohort_id,
    )


def _resolve_report(*, request: Any, source_id: int) -> ResolvedPrintSource:
    from apps.reports.models import ReportRun
    from apps.reports.selectors import scoped_runs

    run = scoped_runs(user=request.user, roles=get_user_roles(request)).filter(pk=source_id).first()
    if run is None:
        _not_found()
    expected_key = _report_key(run)
    if run.status != ReportRun.Status.DONE or run.s3_key != expected_key:
        _not_ready("The report file is not ready.")
    branch_id = _single_report_branch(run.params)
    # ``scoped_runs`` intentionally retains a caller's historical own runs. For
    # printing, current authorization must still cover the immutable report branch;
    # otherwise a reports grant in A could combine with printing authority in B.
    if not request_permission_membership_allows(
        request,
        permission="reports:read",
        branch_id=branch_id,
        enforce_department=False,
    ):
        _not_found()
    cohort_id = _validated_report_cohort(run.params, branch_id=branch_id)
    return ResolvedPrintSource(
        source=PrintJob.Source.REPORT,
        source_id=run.pk,
        payload_s3_key=expected_key,
        branch_id=branch_id,
        cohort_id=cohort_id,
    )


def _resolve_receipt(*, request: Any, source_id: int) -> ResolvedPrintSource:
    from apps.payments.models import FiscalReceipt, Payment
    from apps.payments.selectors import payments_qs

    payments = scope_to_permission_memberships(
        request,
        payments_qs().select_related("fiscal_receipt"),
        permission="finance:read",
        branch_field="branch_at_payment_id",
        department_field="department_at_payment_id",
        account_kinds={"staff"},
    )
    payment = payments.filter(pk=source_id).first()
    if payment is None:
        _not_found()
    receipt = getattr(payment, "fiscal_receipt", None)
    expected_key = _receipt_key(payment.pk)
    if (
        receipt is None
        or payment.status != Payment.Status.COMPLETED
        or receipt.status != FiscalReceipt.Status.CONFIRMED
        or receipt.pdf_key != expected_key
    ):
        _not_ready("The receipt file is not ready.")
    if payment.branch_at_payment_id is None:  # excluded by payments_qs; defense in depth
        _not_ready("The receipt has no authoritative branch attribution.")
    return ResolvedPrintSource(
        source=PrintJob.Source.RECEIPT,
        source_id=payment.pk,
        payload_s3_key=expected_key,
        branch_id=payment.branch_at_payment_id,
    )


def _content_library(file):
    if file.folder_id is not None:
        return file.folder.library
    if file.lesson_id is not None:
        return file.lesson.module.course.library
    return None


def _content_library_branch(library) -> int | None:
    from apps.content.models import ContentLibrary

    if library.visibility == ContentLibrary.Visibility.DEPARTMENT and library.department_id:
        return library.department.branch_id
    if library.visibility == ContentLibrary.Visibility.COHORT and library.cohort_id:
        return library.cohort.branch_id
    return None


def _resolve_content_file(*, request: Any, source_id: int) -> ResolvedPrintSource:
    from apps.content.models import LessonFile
    from apps.content.selectors import scoped_files
    from apps.content.storage_keys import trusted_primary_key
    from apps.printing.document_inspection import MAX_PRINT_DOCUMENT_BYTES

    file = (
        scoped_files(user=request.user, roles=get_user_roles(request), permission="content:read")
        .select_related(
            "folder__library__department",
            "folder__library__cohort",
            "lesson__module__course__library__department",
            "lesson__module__course__library__cohort",
        )
        .filter(pk=source_id)
        .first()
    )
    if file is None:
        _not_found()
    if (
        file.status != LessonFile.Status.CLEAN
        or not file.is_downloadable
        or file.content_type not in _PRINTABLE_CONTENT_TYPES
        or file.size_bytes < 1
        or file.size_bytes > MAX_PRINT_DOCUMENT_BYTES
    ):
        _not_ready("The selected library file is not available for printing.")
    key = trusted_primary_key(file, schema=current_schema())
    if key is None:
        _not_ready("The selected library file has an invalid storage reference.")
    library = _content_library(file)
    if library is None:
        _not_ready("The selected library file has no content location.")
    return ResolvedPrintSource(
        source=PrintJob.Source.CONTENT,
        source_id=file.pk,
        payload_s3_key=key,
        branch_id=_content_library_branch(library),
        cohort_id=library.cohort_id,
        content_type=file.content_type,
        size_bytes=file.size_bytes,
    )


def is_print_job_source_valid(job: PrintJob) -> bool:
    """Fail closed unless a claimed job still matches its authoritative source.

    This is intentionally independent of the original caller's session: creation
    already enforced caller scope, while claim-time validation protects against
    legacy rows, direct internal misuse, and a source/key/branch changed after queueing.
    """

    if job.source == PrintJob.Source.ASSIGNMENT:
        from apps.assignments.models import Assignment
        from apps.assignments.services import trusted_attachment_keys

        # The claim endpoint holds an outer transaction around this check and URL
        # signing. Lock the authoritative source (and its non-null cohort join) so
        # it cannot be moved, deleted, or have its attachment replaced in the gap
        # between validation and capability issuance.
        assignment = (
            Assignment.objects.select_related(None)
            .select_related("cohort")
            .select_for_update()
            .filter(pk=job.source_id)
            .first()
        )
        return bool(
            assignment is not None
            and assignment.cohort.branch_id == job.branch_id
            and assignment.cohort_id == job.cohort_id
            and isinstance(assignment.attachments, list)
            and job.payload_s3_key in trusted_attachment_keys(assignment)
            and _is_assignment_key(job.payload_s3_key)
        )

    if job.source == PrintJob.Source.TRANSCRIPT:
        from apps.academics.models import Transcript

        transcript = (
            Transcript.objects.select_related(None)
            .select_related("student")
            .select_for_update()
            .filter(pk=job.source_id)
            .first()
        )
        expected_key = _transcript_key(job.source_id)
        return bool(
            transcript is not None
            and transcript.status == Transcript.Status.DONE
            and transcript.student.branch_id == job.branch_id
            and transcript.student.current_cohort_id == job.cohort_id
            and transcript.pdf_key == expected_key
            and job.payload_s3_key == expected_key
        )

    if job.source == PrintJob.Source.REPORT:
        from apps.reports.models import ReportRun

        run = ReportRun.objects.select_for_update().filter(pk=job.source_id).first()
        if run is None or run.status != ReportRun.Status.DONE:
            return False
        try:
            expected_key = _report_key(run)
            branch_id = _single_report_branch(run.params)
            cohort_id = _validated_report_cohort(run.params, branch_id=branch_id)
        except UnprocessableEntity:
            return False
        return (
            run.s3_key == expected_key == job.payload_s3_key
            and branch_id == job.branch_id
            and cohort_id == job.cohort_id
        )

    if job.source == PrintJob.Source.RECEIPT:
        from apps.payments.models import FiscalReceipt, Payment
        from apps.payments.selectors import payments_qs

        payment = payments_qs().select_related(None).select_for_update().filter(pk=job.source_id).first()
        receipt = (
            FiscalReceipt.objects.select_for_update().filter(payment_id=job.source_id).first()
            if payment is not None
            else None
        )
        expected_key = _receipt_key(job.source_id)
        return bool(
            payment is not None
            and receipt is not None
            and payment.status == Payment.Status.COMPLETED
            and receipt.status == FiscalReceipt.Status.CONFIRMED
            and payment.branch_at_payment_id == job.branch_id
            and receipt.pdf_key == expected_key
            and job.payload_s3_key == expected_key
            and job.cohort_id is None
        )

    if job.source == PrintJob.Source.CONTENT:
        from apps.content.models import LessonFile
        from apps.content.storage_keys import trusted_primary_key

        file = (
            LessonFile.objects.select_related(None)
            .select_related(
                "folder__library__department",
                "folder__library__cohort",
                "lesson__module__course__library__department",
                "lesson__module__course__library__cohort",
            )
            .select_for_update()
            .filter(pk=job.source_id)
            .first()
        )
        if file is None:
            return False
        library = _content_library(file)
        authoritative_branch = _content_library_branch(library) if library is not None else None
        expected_content_key = trusted_primary_key(file, schema=current_schema())
        expected_cohort = library.cohort_id if library is not None else None
        return bool(
            file.status == LessonFile.Status.CLEAN
            and file.is_downloadable
            and file.content_type in _PRINTABLE_CONTENT_TYPES
            and expected_content_key is not None
            and job.payload_s3_key == expected_content_key
            and (authoritative_branch is None or authoritative_branch == job.branch_id)
            and expected_cohort == job.cohort_id
        )

    if job.source == PrintJob.Source.UPLOAD:
        from apps.printing.models import PrintUploadGrant
        from apps.printing.storage_keys import parse_final_print_document_key

        grant = PrintUploadGrant.objects.select_for_update().filter(pk=job.source_id).first()
        if grant is None:
            return False
        parsed = parse_final_print_document_key(grant.durable_key, schema=current_schema())
        return bool(
            grant.consumed_at is not None
            and grant.durable_deleted_at is None
            and grant.branch_id == job.branch_id
            and grant.requested_by_id == job.requested_by_id
            and grant.durable_key == job.payload_s3_key
            and parsed is not None
            and parsed.grant_id == grant.pk
            and parsed.filename == grant.filename
            and job.cohort_id is None
        )

    return False


def _is_assignment_key(value: object) -> bool:
    from apps.assignments.storage_keys import parse_final_attachment_key, parse_legacy_attachment_key

    schema = current_schema()
    return (
        parse_final_attachment_key(value, schema=schema) is not None
        or parse_legacy_attachment_key(value, schema=schema) is not None
    )


def _transcript_key(source_id: int) -> str:
    return f"{current_schema()}/transcripts/{source_id}.pdf"


def _report_key(run: Any) -> str:
    if run.format == "xlsx":
        extension = "xlsx"
    elif run.format == "pdf":
        extension = "pdf"
    else:
        _not_ready("The report format is not printable.")
    return f"{current_schema()}/reports/{run.pk}.{extension}"


def _receipt_key(source_id: int) -> str:
    return f"{current_schema()}/receipts/{source_id}.pdf"


def _single_report_branch(params: object) -> int:
    raw = params.get("_scope_branch_ids") if isinstance(params, dict) else None
    if (
        not isinstance(raw, list)
        or len(raw) != 1
        or isinstance(raw[0], bool)
        or not isinstance(raw[0], int)
        or raw[0] < 1
    ):
        _not_ready("Only a report scoped to one branch can be printed by a branch agent.")
    branch_id = raw[0]
    from apps.org.models import Branch

    if not Branch.objects.filter(pk=branch_id).exists():
        _not_ready("The report branch no longer exists.")
    return branch_id


def _validated_report_cohort(params: object, *, branch_id: int) -> int | None:
    raw = params.get("cohort_id") if isinstance(params, dict) else None
    if raw in (None, ""):
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        _not_ready("The report cohort attribution is invalid.")
    from apps.cohorts.models import Cohort

    if not Cohort.objects.filter(pk=raw, branch_id=branch_id).exists():
        _not_ready("The report cohort is outside its branch scope.")
    return raw


def _not_found() -> Never:
    raise NotFoundException(_("Print source not found."), code="not_found")


def _not_ready(message: str) -> Never:
    raise UnprocessableEntity(_(message), code="print_source_not_ready")
