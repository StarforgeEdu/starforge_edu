"""Printing endpoints — plain Django views over the layered architecture.

Two surfaces: STAFF (JWT, printing:read/write) manage jobs/printers/agents (branch object-
scoped: create + detail scoped to the actor's branch, list is whole-tenant operational data);
AGENT (a BranchAgent token, NOT a User — via @require_branch_agent) claims jobs + reports
status. No PUT/DELETE on jobs; printers allow PATCH; agents add a revoke action.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.views.decorators.csrf import csrf_exempt

from apps.org.models import Branch
from apps.printing.agent_auth import require_branch_agent
from apps.printing.interfaces.services import (
    IBranchAgentService,
    IPrinterService,
    IPrintJobService,
)
from apps.printing.models import PrintJob
from apps.printing.presenters import (
    branch_agent_created_to_dict,
    branch_agent_to_dict,
    print_job_to_dict,
    printer_to_dict,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import Role, get_role_memberships
from core.responses import created, error, no_content, paginated, success
from core.viewsets import assert_tenant_context
from infrastructure.storage.s3_client import presign_download

_SOURCES = set(PrintJob.Source.values)
_AGENT_STATUSES = {
    PrintJob.Status.PRINTING.value,
    PrintJob.Status.DONE.value,
    PrintJob.Status.FAILED.value,
}


def _job_service() -> IPrintJobService:
    return container.resolve(IPrintJobService)  # type: ignore[type-abstract]


def _printer_service() -> IPrinterService:
    return container.resolve(IPrinterService)  # type: ignore[type-abstract]


def _agent_service() -> IBranchAgentService:
    return container.resolve(IBranchAgentService)  # type: ignore[type-abstract]


# --- staff: print jobs -----------------------------------------------------
@csrf_exempt
@require_auth
def jobs_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "printing:read")
        qs = apply_filters(
            request,
            _job_service().list_jobs(),
            filter_fields=("status", "source", "branch"),
            ordering_fields=("created_at",),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([print_job_to_dict(j) for j in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "printing:write")
        return _create_job(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def job_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "printing:read")
    job = _job_service().get(pk=pk)
    if job is None:
        raise NotFoundException(code="not_found")
    _assert_in_branch(request, job.branch_id)
    return success(print_job_to_dict(job))


# --- staff: printers -------------------------------------------------------
@csrf_exempt
@require_auth
def printers_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "printing:read")
        qs = apply_filters(
            request,
            _printer_service().list_printers(),
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
    check_perm(request, "printing:read" if read else "printing:write")
    printer = _printer_service().get(pk=pk)
    if printer is None:
        raise NotFoundException(code="not_found")
    _assert_in_branch(request, printer.branch_id)
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
            _agent_service().list_agents(),
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
    agent = _agent_service().get(pk=pk)
    if agent is None:
        raise NotFoundException(code="not_found")
    _assert_in_branch(request, agent.branch_id)
    return success(branch_agent_to_dict(agent))


@csrf_exempt
@require_auth
def agent_revoke_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "printing:write")
    agent = _agent_service().get(pk=pk)
    if agent is None:
        raise NotFoundException(code="not_found")
    _assert_in_branch(request, agent.branch_id)
    return success(branch_agent_to_dict(_agent_service().revoke(agent)))


# --- agent surface (BranchAgent token, no JWT) -----------------------------
@csrf_exempt
@require_branch_agent
def agent_claim_view(request: HttpRequest) -> HttpResponseBase:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    assert_tenant_context()
    job = _job_service().claim(agent=request.auth)  # type: ignore[attr-defined]
    if job is None:
        return no_content()  # queue empty -> 204
    return success({"job": print_job_to_dict(job), "download_url": presign_download(job.payload_s3_key)})


@csrf_exempt
@require_branch_agent
def agent_job_status_view(request: HttpRequest, job_id: int) -> HttpResponseBase:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    assert_tenant_context()
    body = read_json(request)
    job = _job_service().update_status(
        agent=request.auth,  # type: ignore[attr-defined]
        job_id=job_id,
        status=_choice(body, "status", _AGENT_STATUSES),
        error=str_field(body, "error", max_length=2000),
        pages_printed=_optional_nonneg_int(body, "pages_printed"),
    )
    return success(print_job_to_dict(job))


# --- helpers ---------------------------------------------------------------
def _create_job(request: HttpRequest) -> HttpResponse:
    from core.utils import current_schema

    body = read_json(request)
    payload_key = str_field(body, "payload_s3_key", max_length=512).strip()
    if not payload_key:
        raise ValidationException(
            "payload_s3_key is required.",
            code="validation_error",
            fields={"payload_s3_key": ["This field is required."]},
        )
    # Tenant isolation: this key is echoed into a presigned S3 GET at agent-claim time
    # (agent_claim_view -> presign_download), against the ONE shared bucket keyed only by
    # a "{schema}/..." prefix convention. An unvalidated client key lets a branch-scoped
    # staffer mint a working presigned download URL for ANY object in the bucket —
    # cross-tenant AND cross-permission file exfiltration. Require the caller's own tenant
    # prefix, mirroring the assignments attachment-key guard. (Internal transcript/receipt/
    # report hand-offs call enqueue_print at the service layer, bypassing this HTTP path.)
    prefix = f"{current_schema()}/"
    if not payload_key.startswith(prefix):
        raise ValidationException(
            "payload_s3_key is not valid for this tenant.",
            code="validation_error",
            fields={"payload_s3_key": [f"Key must start with '{prefix}'."]},
        )
    branch_id = _required_pos_int(body, "branch")
    _assert_branch_write(request, branch_id)
    source = _choice(body, "source", _SOURCES)
    # Cross-permission guard (R4/PLAUS1): the payload key is echoed into a presigned
    # download at claim time, so a print job for a sensitive server-generated document
    # (transcript / payment receipt / report) discloses that document to whoever claims
    # it. Require the caller to hold the OWNING resource's READ permission — printing:write
    # alone must not let e.g. a registrar/security/librarian pull finance receipts or
    # academic transcripts they cannot otherwise read. (Object-level scope + deriving the
    # key from source_id instead of trusting the client key is the tracked follow-up.)
    _source_read_perm: dict[str, str] = {
        PrintJob.Source.TRANSCRIPT: "academics:read",
        PrintJob.Source.REPORT: "reports:read",
        PrintJob.Source.RECEIPT: "finance:read",
        PrintJob.Source.ASSIGNMENT: "assignments:read",
    }
    check_perm(request, _source_read_perm[source])
    data = {
        "source": source,
        "source_id": _required_pos_int(body, "source_id"),
        "payload_s3_key": payload_key,
        "branch": branch_id,
        "pages": _required_pos_int(body, "pages"),
        "copies": _positive_default(body, "copies", 1),
        "color": bool_field(body, "color", default=False),
        "duplex": bool_field(body, "duplex", default=False),
        "cohort": _optional_pos_int(body, "cohort"),
    }
    return created(print_job_to_dict(_job_service().enqueue(data=data, requested_by=request.user)))


def _create_printer(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    branch_id = _required_pos_int(body, "branch")
    if Branch.objects.filter(pk=branch_id).first() is None:
        raise ValidationException(
            "Unknown branch.", code="validation_error", fields={"branch": ["No such branch."]}
        )
    name = str_field(body, "name", max_length=120).strip()
    if not name:
        raise ValidationException(
            "name is required.", code="validation_error", fields={"name": ["This field is required."]}
        )
    _assert_branch_write(request, branch_id)
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
    """Branch object-scope on a write: superuser/DIRECTOR unscoped; else the branch must be
    one of the actor's role-membership branches (mirrors the old _assert_create_branch /
    ObjectScopedPermission -> 403 out_of_scope)."""
    if getattr(request.user, "is_superuser", False):
        return
    req: Any = request
    memberships = list(get_role_memberships(req))
    if any(m.role == Role.DIRECTOR for m in memberships):
        return
    if branch_id not in {m.branch_id for m in memberships}:
        raise PermissionException(code="out_of_scope")


def _assert_in_branch(request: HttpRequest, branch_id: int) -> None:
    _assert_branch_write(request, branch_id)


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


def _optional_pos_int(body: dict[str, Any], name: str) -> int | None:
    if body.get(name) is None:
        return None
    return _required_pos_int(body, name)


def _optional_nonneg_int(body: dict[str, Any], name: str) -> int | None:
    if body.get(name) is None:
        return None
    value = int_field(body, name, required=True)
    if value is None or value < 0:
        raise ValidationException(
            f"{name} must be a non-negative integer.",
            code="validation_error",
            fields={name: ["Must be an integer >= 0."]},
        )
    return value


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
