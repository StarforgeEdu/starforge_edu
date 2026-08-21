"""Printing endpoints — plain Django views over the layered architecture.

Two surfaces: STAFF (JWT, printing:read/write) manage jobs/printers/agents within the
branches covered by the exact membership granting that permission; DIRECTOR/superuser
remain organization-wide. AGENT (a BranchAgent token, NOT a User — via
``@require_branch_agent``) claims jobs + reports status. No PUT/DELETE on jobs; printers
allow PATCH; agents add a revoke action.
"""

from __future__ import annotations

import logging
import math
from typing import Any
from uuid import UUID

from botocore.exceptions import BotoCoreError
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.org.models import Branch
from apps.printing import services as printing_domain
from apps.printing.agent_auth import require_branch_agent
from apps.printing.interfaces.services import (
    IBranchAgentService,
    IPrinterService,
    IPrintJobService,
)
from apps.printing.models import PrintJob, PrintJobReconciliation, PrintUploadGrant
from apps.printing.openapi_contracts import (
    AGENT_CLAIM_CONTRACTS,
    AGENT_JOB_HEARTBEAT_CONTRACTS,
    AGENT_JOB_STATUS_CONTRACTS,
    JOB_RECONCILE_CONTRACTS,
    JOB_RECONCILIATIONS_CONTRACTS,
    JOBS_COLLECTION_CONTRACTS,
    PRINT_UPLOAD_CONTRACTS,
)
from apps.printing.presenters import (
    agent_print_job_to_dict,
    branch_agent_created_to_dict,
    branch_agent_to_dict,
    print_job_reconciliation_to_dict,
    print_job_to_dict,
    printer_to_dict,
)
from apps.printing.source_resolver import (
    is_print_job_source_valid,
    resolve_print_source,
    source_read_permission,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import (
    ConflictException,
    NotFoundException,
    ServiceUnavailableException,
    UnprocessableEntity,
    ValidationException,
)
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.openapi_contracts import openapi_contract
from core.responses import created, error, no_content, paginated, success
from core.role_principals import request_role_principal
from core.scoping import assert_permission_membership_scope, scope_to_permission_memberships
from core.tenant_context import assert_tenant_context
from infrastructure.storage.s3_client import presign_download

_SOURCES = set(PrintJob.Source.values)
_AGENT_STATUSES = {
    PrintJob.Status.PRINTING.value,
    PrintJob.Status.DONE.value,
    PrintJob.Status.FAILED.value,
}
_RECONCILIATION_OUTCOMES = set(PrintJobReconciliation.Outcome.values)
logger = logging.getLogger("starforge.printing")


def _job_service() -> IPrintJobService:
    return container.resolve(IPrintJobService)  # type: ignore[type-abstract]


def _printer_service() -> IPrinterService:
    return container.resolve(IPrinterService)  # type: ignore[type-abstract]


def _agent_service() -> IBranchAgentService:
    return container.resolve(IBranchAgentService)  # type: ignore[type-abstract]


# --- staff: print jobs -----------------------------------------------------
@openapi_contract(
    path="/api/v1/printing/jobs/",
    operations=JOBS_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def jobs_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "printing:read")
        qs = apply_filters(
            request,
            _visible_jobs(request, permission="printing:read"),
            filter_fields=("status", "source", "branch"),
            ordering_fields=("created_at",),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([print_job_to_dict(j) for j in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "printing:write")
        # Parse the declared JSON request at the registered transport boundary.
        # Besides keeping malformed/non-object input fail-closed, this makes the
        # executable callback independently agree with its critical OpenAPI
        # request-body contract; the helper only validates the closed DTO below.
        return _create_job(request, body=read_json(request))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/printing/upload-url/",
    operations=PRINT_UPLOAD_CONTRACTS,
)
@csrf_exempt
@require_auth
def print_upload_url_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "printing:write")
    body = read_json(request)
    unknown_fields = sorted(set(body) - {"branch", "filename", "content_type", "size_bytes"})
    if unknown_fields:
        raise ValidationException(
            "The print upload request contains unsupported fields.",
            code="validation_error",
            fields={field: ["This field is unsupported."] for field in unknown_fields},
        )
    branch_id = _required_pos_int(body, "branch")
    _assert_branch_write(request, branch_id)
    result = _job_service().request_upload(
        data={
            "branch": branch_id,
            "filename": str_field(body, "filename", max_length=255),
            "content_type": str_field(body, "content_type", max_length=127),
            "size_bytes": _required_pos_int(body, "size_bytes"),
        },
        requested_by=request.user,
    )
    return created(result)


@csrf_exempt
@require_auth
def job_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "printing:read")
    job = _visible_jobs(request, permission="printing:read").filter(pk=pk).first()
    if job is None:
        raise NotFoundException(code="not_found")
    return success(print_job_to_dict(job))


@openapi_contract(
    path="/api/v1/printing/jobs/{pk}/reconciliations/",
    operations=JOB_RECONCILIATIONS_CONTRACTS,
)
@csrf_exempt
@require_auth
def job_reconciliations_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "printing:read")
    job = _visible_jobs(request, permission="printing:read").filter(pk=pk).first()
    if job is None:
        raise NotFoundException(code="not_found")
    items, total, page, size = paginate(
        request,
        _job_service().list_reconciliations(job_id=job.pk),
    )
    return paginated(
        [print_job_reconciliation_to_dict(record) for record in items],
        total=total,
        page=page,
        page_size=size,
    )


@openapi_contract(
    path="/api/v1/printing/jobs/{pk}/reconcile/",
    operations=JOB_RECONCILE_CONTRACTS,
)
@csrf_exempt
@require_auth
def job_reconcile_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "printing:write")
    job = _visible_jobs(request, permission="printing:write").filter(pk=pk).first()
    if job is None:
        raise NotFoundException(code="not_found")
    body = read_json(request)
    unknown_fields = sorted(set(body) - {"outcome", "evidence_reference"})
    if unknown_fields:
        raise ValidationException(
            "The reconciliation request contains unsupported fields.",
            code="validation_error",
            fields={field: ["This field is unsupported."] for field in unknown_fields},
        )
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is None:
        raise ValidationException(
            "Idempotency-Key is required.",
            code="validation_error",
            fields={"Idempotency-Key": ["This header is required."]},
        )
    reconciled = _job_service().reconcile(
        job_id=job.pk,
        expected_branch_id=job.branch_id,
        actor=request.user,
        actor_principal=request_role_principal(
            request,
            allowed_kinds={"staff"},
            error_code="printing_principal_unavailable",
        ),
        outcome=_choice(body, "outcome", _RECONCILIATION_OUTCOMES),
        evidence_reference=str_field(body, "evidence_reference", max_length=200),
        idempotency_key=idempotency_key,
    )
    return success(print_job_to_dict(reconciled))


# --- staff: printers -------------------------------------------------------
@csrf_exempt
@require_auth
def printers_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "printing:read")
        qs = apply_filters(
            request,
            _visible_printers(request, permission="printing:read"),
            filter_fields=("branch", "is_active"),
            ordering_fields=("name",),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([printer_to_dict(p) for p in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "printing:write")
        return _create_printer(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def printer_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    permission = "printing:read" if read else "printing:write"
    check_perm(request, permission)
    printer = _visible_printers(request, permission=permission).filter(pk=pk).first()
    if printer is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(printer_to_dict(printer))
    if request.method == "PATCH":
        return success(printer_to_dict(_printer_service().update(printer, _printer_changes(request))))
    return error("Method not allowed.", code="method_not_allowed", status=405)


# --- staff: branch agents --------------------------------------------------
@csrf_exempt
@require_auth
def agents_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "printing:read")
        qs = apply_filters(
            request,
            _visible_agents(request, permission="printing:read"),
            filter_fields=("branch",),
            ordering_fields=("name",),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([branch_agent_to_dict(a) for a in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "printing:write")
        return _register_agent(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def agent_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "printing:read")
    agent = _visible_agents(request, permission="printing:read").filter(pk=pk).first()
    if agent is None:
        raise NotFoundException(code="not_found")
    return success(branch_agent_to_dict(agent))


@csrf_exempt
@require_auth
def agent_revoke_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "printing:write")
    agent = _visible_agents(request, permission="printing:write").filter(pk=pk).first()
    if agent is None:
        raise NotFoundException(code="not_found")
    return success(branch_agent_to_dict(_agent_service().revoke(agent)))


# --- agent surface (BranchAgent token, no JWT) -----------------------------
@openapi_contract(
    path="/api/v1/printing/agent/claim/",
    operations=AGENT_CLAIM_CONTRACTS,
)
@csrf_exempt
@require_branch_agent
@transaction.atomic
def agent_claim_view(request: HttpRequest) -> HttpResponseBase:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    assert_tenant_context()
    body = read_json(request)
    if body:
        raise ValidationException(
            "The claim request does not accept fields.",
            code="validation_error",
            fields={field: ["This field is unsupported."] for field in sorted(body)},
        )
    job = _job_service().claim(agent=request.auth)  # type: ignore[attr-defined]
    if job is None:
        return no_content()  # queue empty -> 204
    if not is_print_job_source_valid(job):
        _job_service().reject_invalid_claim(agent=request.auth, job_id=job.pk)  # type: ignore[attr-defined]
        return error(
            "The print source is no longer available.",
            code="print_source_invalid",
            status=409,
        )
    try:
        if job.lease_expires_at is None:
            raise ImproperlyConfigured("Claimed print job has no lease expiry.")
        remaining_lease_seconds = max(
            1,
            math.ceil((job.lease_expires_at - timezone.now()).total_seconds()),
        )
        # The storage capability never outlives the physical-attempt lease.
        # A heartbeat can extend processing time after the document is already
        # downloaded, but it does not mint another download capability.
        download_url = presign_download(
            job.payload_s3_key,
            expires_in=remaining_lease_seconds,
        )
    except (BotoCoreError, ImproperlyConfigured, KeyError, ValueError) as exc:
        # The enclosing transaction rolls back PICKED/agent/printer state. Never
        # log the object key or exception text: storage configuration can contain
        # endpoint or credential material and the request id is enough to correlate.
        logger.error("Unable to issue a branch-agent print download capability.")
        raise ServiceUnavailableException(
            "The print document is temporarily unavailable.",
            code="print_download_unavailable",
        ) from exc
    return success({"job": agent_print_job_to_dict(job), "download_url": download_url})


@openapi_contract(
    path="/api/v1/printing/agent/jobs/{job_id}/status/",
    operations=AGENT_JOB_STATUS_CONTRACTS,
)
@csrf_exempt
@require_branch_agent
def agent_job_status_view(request: HttpRequest, job_id: int) -> HttpResponseBase:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    assert_tenant_context()
    body = read_json(request)
    unknown_fields = sorted(set(body) - {"lease_id", "status", "error", "pages_printed"})
    if unknown_fields:
        raise ValidationException(
            "The status report contains unsupported fields.",
            code="validation_error",
            fields={field: ["This field is unsupported."] for field in unknown_fields},
        )
    status = _choice(body, "status", _AGENT_STATUSES)
    if "error" in body and body["error"] is None:
        raise ValidationException(
            "error may not be null.",
            code="validation_error",
            fields={"error": ["This field may not be null."]},
        )
    reported_error = str_field(body, "error", max_length=2000)
    if reported_error and status != PrintJob.Status.FAILED:
        raise ValidationException(
            "An error may be reported only with failed status.",
            code="validation_error",
            fields={"error": ["This field is allowed only when status is failed."]},
        )
    job = _job_service().update_status(
        agent=request.auth,  # type: ignore[attr-defined]
        job_id=job_id,
        lease_id=_required_uuid(body, "lease_id"),
        status=status,
        error=reported_error,
        pages_printed=_optional_nonneg_int(body, "pages_printed"),
    )
    if job.status == PrintJob.Status.RECONCILIATION_REQUIRED:
        raise ConflictException(
            "The print attempt requires operator reconciliation.",
            code="print_reconciliation_required",
        )
    return success(agent_print_job_to_dict(job))


@openapi_contract(
    path="/api/v1/printing/agent/jobs/{job_id}/heartbeat/",
    operations=AGENT_JOB_HEARTBEAT_CONTRACTS,
)
@csrf_exempt
@require_branch_agent
def agent_job_heartbeat_view(request: HttpRequest, job_id: int) -> HttpResponseBase:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    assert_tenant_context()
    body = read_json(request)
    unknown_fields = sorted(set(body) - {"lease_id", "pages_printed"})
    if unknown_fields:
        raise ValidationException(
            "The heartbeat contains unsupported fields.",
            code="validation_error",
            fields={field: ["This field is unsupported."] for field in unknown_fields},
        )
    job = _job_service().heartbeat(
        agent=request.auth,  # type: ignore[attr-defined]
        job_id=job_id,
        lease_id=_required_uuid(body, "lease_id"),
        pages_printed=_optional_nonneg_int(body, "pages_printed"),
    )
    if job.status == PrintJob.Status.RECONCILIATION_REQUIRED:
        raise ConflictException(
            "The print attempt requires operator reconciliation.",
            code="print_reconciliation_required",
        )
    return success(agent_print_job_to_dict(job))


# --- helpers ---------------------------------------------------------------
def _create_job(request: HttpRequest, *, body: dict[str, Any]) -> HttpResponse:
    allowed_fields = {
        "source",
        "source_id",
        "attachment_index",
        "pages",
        "copies",
        "color",
        "duplex",
        "printer",
        "scheduled_for",
    }
    unknown_fields = sorted(set(body) - allowed_fields)
    if unknown_fields:
        raise ValidationException(
            "The print request contains unsupported fields.",
            code="validation_error",
            fields={name: ["This field is server-managed or unsupported."] for name in unknown_fields},
        )
    source = _choice(body, "source", _SOURCES)
    source_id = _required_pos_int(body, "source_id")
    source_permission = source_read_permission(source)
    check_perm(request, source_permission)
    printer_id = _optional_pos_int(body, "printer")
    scheduled_for = _optional_schedule(body)

    if source == PrintJob.Source.UPLOAD:
        if "attachment_index" in body:
            raise ValidationException(
                "attachment_index is only valid for assignment sources.",
                code="validation_error",
                fields={"attachment_index": ["Remove this field for an uploaded print file."]},
            )
        if printer_id is None:
            raise ValidationException(
                "printer is required for an uploaded print file.",
                code="validation_error",
                fields={"printer": ["Choose an available printer."]},
            )
        grant = (
            PrintUploadGrant.objects.filter(
                pk=source_id,
                requested_by_id=getattr(request.user, "pk", None),
            )
            .only("branch_id")
            .first()
        )
        if grant is None:
            raise NotFoundException(code="not_found")
        printer = _visible_printers(request, permission="printing:write").filter(pk=printer_id).first()
        if printer is None:
            raise NotFoundException(code="not_found")
        if printer.branch_id != grant.branch_id:
            raise ValidationException(
                "The selected printer is outside the upload branch.",
                code="printer_source_branch_mismatch",
                fields={"printer": ["Choose a printer in the upload branch."]},
            )
        assert_permission_membership_scope(
            request,
            permission="printing:write",
            branch_id=grant.branch_id,
            enforce_department=False,
        )
        data = {
            "source_id": source_id,
            "pages": _optional_pos_int(body, "pages"),
            "copies": _positive_default(body, "copies", 1),
            "color": bool_field(body, "color", default=False),
            "duplex": bool_field(body, "duplex", default=False),
            "printer": printer_id,
            "scheduled_for": scheduled_for,
        }
        return created(print_job_to_dict(_job_service().enqueue_upload(data=data, requested_by=request.user)))

    resolved = resolve_print_source(
        request=request,
        source=source,
        source_id=source_id,
        attachment_index=_optional_nonneg_int(body, "attachment_index"),
    )
    printer = None
    if printer_id is not None:
        printer = _visible_printers(request, permission="printing:write").filter(pk=printer_id).first()
        if printer is None:
            raise NotFoundException(code="not_found")
    if source == PrintJob.Source.CONTENT and printer is None:
        raise ValidationException(
            "printer is required for a library file.",
            code="validation_error",
            fields={"printer": ["Choose an available printer."]},
        )
    branch_id = resolved.branch_id if resolved.branch_id is not None else getattr(printer, "branch_id", None)
    if branch_id is None:
        raise ValidationException(
            "The print source has no branch route.",
            code="validation_error",
            fields={"printer": ["Choose an available printer."]},
        )
    if printer is not None and printer.branch_id != branch_id:
        raise ValidationException(
            "The selected printer is outside the source branch.",
            code="printer_source_branch_mismatch",
            fields={"printer": ["Choose a printer in the document's branch."]},
        )
    requested_pages = _optional_pos_int(body, "pages")
    if source == PrintJob.Source.CONTENT:
        if resolved.content_type is None or resolved.size_bytes is None:
            raise UnprocessableEntity(
                "The selected library file has no printable metadata.",
                code="print_source_not_ready",
            )
        pages = printing_domain.authoritative_print_pages(
            key=resolved.payload_s3_key,
            content_type=resolved.content_type,
            size_bytes=resolved.size_bytes,
            requested_pages=requested_pages,
        )
    else:
        pages = _required_pos_int(body, "pages")
    # The source query proves read scope. Independently require that the exact
    # membership supplying printing:write covers the source's authoritative branch.
    assert_permission_membership_scope(
        request,
        permission="printing:write",
        branch_id=branch_id,
        enforce_department=False,
    )
    data = {
        "source": resolved.source,
        "source_id": resolved.source_id,
        "payload_s3_key": resolved.payload_s3_key,
        "branch": branch_id,
        "pages": pages,
        "copies": _positive_default(body, "copies", 1),
        "color": bool_field(body, "color", default=False),
        "duplex": bool_field(body, "duplex", default=False),
        "cohort": resolved.cohort_id,
        "printer": printer_id,
        "scheduled_for": scheduled_for,
    }
    return created(print_job_to_dict(_job_service().enqueue(data=data, requested_by=request.user)))


def _create_printer(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    branch_id = _required_pos_int(body, "branch")
    _assert_branch_write(request, branch_id)
    if Branch.objects.filter(pk=branch_id).first() is None:
        raise ValidationException(
            "Unknown branch.", code="validation_error", fields={"branch": ["No such branch."]}
        )
    name = str_field(body, "name", max_length=120).strip()
    if not name:
        raise ValidationException(
            "name is required.", code="validation_error", fields={"name": ["This field is required."]}
        )
    printer = _printer_service().create(
        data={
            "branch_id": branch_id,
            "name": name,
            "model_name": str_field(body, "model_name", max_length=120),
            "capabilities": _capabilities(body),
            "is_active": bool_field(body, "is_active", default=True),
        }
    )
    return created(printer_to_dict(printer))


def _printer_changes(request: HttpRequest) -> dict[str, Any]:
    body = read_json(request)
    # These columns are NOT NULL: an explicit JSON null must be a 400, not a silent
    # coerce-to-default that would wipe capabilities / disable the printer / blank the
    # model (the old PrinterWriteSerializer's non-null fields returned 400).
    for field in ("name", "model_name", "capabilities", "is_active"):
        if field in body and body[field] is None:
            raise ValidationException(
                f"{field} may not be null.",
                code="validation_error",
                fields={field: ["This field may not be null."]},
            )
    changes: dict[str, Any] = {}
    if "name" in body:
        name = str_field(body, "name", max_length=120).strip()
        if not name:
            raise ValidationException(
                "name may not be blank.", code="validation_error", fields={"name": ["May not be blank."]}
            )
        changes["name"] = name
    if "model_name" in body:
        changes["model_name"] = str_field(body, "model_name", max_length=120)
    if "capabilities" in body:
        changes["capabilities"] = _capabilities(body)
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active")
    return changes


def _register_agent(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    branch_id = _required_pos_int(body, "branch")
    name = str_field(body, "name", max_length=120).strip()
    if not name:
        raise ValidationException(
            "name is required.", code="validation_error", fields={"name": ["This field is required."]}
        )
    _assert_branch_write(request, branch_id)
    agent, raw_token = _agent_service().register(branch_id=branch_id, name=name, created_by=request.user)
    return created(branch_agent_created_to_dict(agent, raw_token))


def _assert_branch_write(request: HttpRequest, branch_id: int) -> None:
    """Require the exact active membership supplying ``printing:write``.

    Printing resources only carry a branch relationship. A department-scoped printing
    membership therefore resolves to that membership's branch, the narrowest boundary
    the model can enforce. Reads use the equivalent query-level scope below.
    """
    assert_permission_membership_scope(
        request,
        permission="printing:write",
        branch_id=branch_id,
        enforce_department=False,
    )


def _visible_jobs(request: HttpRequest, *, permission: str):
    return scope_to_permission_memberships(
        request,
        _job_service().list_jobs(),
        permission=permission,
        branch_field="branch_id",
    )


def _visible_printers(request: HttpRequest, *, permission: str):
    return scope_to_permission_memberships(
        request,
        _printer_service().list_printers(),
        permission=permission,
        branch_field="branch_id",
    )


def _visible_agents(request: HttpRequest, *, permission: str):
    return scope_to_permission_memberships(
        request,
        _agent_service().list_agents(),
        permission=permission,
        branch_field="branch_id",
    )


def _choice(body: dict[str, Any], name: str, valid: set[str]) -> str:
    value = str_field(body, name, max_length=32)
    if value not in valid:
        raise ValidationException(
            f"{name} is not a valid choice.",
            code="validation_error",
            fields={name: [f"Must be one of: {sorted(valid)}."]},
        )
    return value


def _required_pos_int(body: dict[str, Any], name: str) -> int:
    value = int_field(body, name, required=True)
    if value is None or value < 1:
        raise ValidationException(
            f"{name} must be a positive integer.",
            code="validation_error",
            fields={name: ["Must be an integer >= 1."]},
        )
    return value


def _positive_default(body: dict[str, Any], name: str, default: int) -> int:
    value = int_field(body, name, default=default)
    if value is None or value < 1:
        raise ValidationException(
            f"{name} must be a positive integer.",
            code="validation_error",
            fields={name: ["Must be an integer >= 1."]},
        )
    return value


def _optional_nonneg_int(body: dict[str, Any], name: str) -> int | None:
    if name not in body:
        return None
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationException(
            f"{name} must be a non-negative integer.",
            code="validation_error",
            fields={name: ["Must be an integer >= 0."]},
        )
    return value


def _optional_pos_int(body: dict[str, Any], name: str) -> int | None:
    if name not in body:
        return None
    return _required_pos_int(body, name)


def _optional_schedule(body: dict[str, Any]):
    if "scheduled_for" not in body or body["scheduled_for"] is None:
        return None
    value = body["scheduled_for"]
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationException(
            "scheduled_for must be an ISO 8601 date-time.",
            code="validation_error",
            fields={"scheduled_for": ["Provide a date-time with an explicit timezone offset."]},
        )
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise ValidationException(
            "scheduled_for must include a timezone.",
            code="validation_error",
            fields={"scheduled_for": ["Provide a date-time with an explicit timezone offset."]},
        )
    return parsed


def _required_uuid(body: dict[str, Any], name: str) -> UUID:
    value = body.get(name)
    if not isinstance(value, str):
        raise ValidationException(
            f"{name} must be a UUID string.",
            code="validation_error",
            fields={name: ["This field is required and must be a UUID."]},
        )
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValidationException(
            f"{name} must be a UUID string.",
            code="validation_error",
            fields={name: ["Must be a canonical UUID."]},
        ) from exc
    if str(parsed) != value:
        raise ValidationException(
            f"{name} must be a canonical UUID string.",
            code="validation_error",
            fields={name: ["Must be a lowercase hyphenated UUID."]},
        )
    return parsed


def _capabilities(body: dict[str, Any]) -> dict:
    raw = body.get("capabilities")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationException(
            "capabilities must be an object.",
            code="validation_error",
            fields={"capabilities": ["Must be a JSON object."]},
        )
    return raw
