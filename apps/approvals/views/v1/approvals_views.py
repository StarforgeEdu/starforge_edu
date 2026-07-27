"""Approvals + ledger HTTP views (layered, off DRF).

The A-1 approvals engine: anyone with approvals:write may request; approvers
(approvals:approve) approve/reject; the requester may cancel their own; cashiers
(approvals:disburse) pay out -> an append-only ledger row. A requester sees only
their own requests; handlers see all (selectors.scoped_requests). The ledger is
read-only (ledger:read); entries are written only by the services.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.approvals.interfaces.services import IApprovalService, ILedgerService
from apps.approvals.models import LedgerEntry
from apps.approvals.presenters import approval_request_to_dict, ledger_entry_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import decimal_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, paginated, success
from core.viewsets import assert_tenant_context

# Documented request kinds (configured instances of the engine); "other" is the
# escape hatch. (Formerly on the deleted ApprovalRequestCreateSerializer.)
_REQUEST_KINDS = frozenset(
    {
        "expense",
        "loan",
        "procurement",
        "discount",
        "fine",
        "absence_deduction",
        "payment_delay",
        # NOTE: "salary_prep" is deliberately NOT here. A salary is real money OUT whose
        # amount must be COMPUTED from the teacher's PayoutPolicy and branch-scoped — only
        # apps.teachers.prepare_salary may create it (via the domain create_request). Letting
        # the generic POST /approvals/ accept it would allow an approvals:write user to mint
        # a salary for an arbitrary teacher at a raw, uncomputed figure (F13-1 self-review).
        "event_split",
        "book_cash",
        # NOTE: "reward" is deliberately NOT here (same reasoning as salary_prep). A cash reward
        # is real money OUT to a named STAFF member; only apps.rewards.grant_reward may create it
        # (via the domain create_request), which pins recipient_id (int) AND derives party_label
        # from the SAME recipient. Exposed on the generic POST /approvals/ a requester could
        # decouple recipient_id (the SoD identity) from party_label (the ledger payee), or send
        # recipient_id as a string, to bypass _assert_not_beneficiary_self_dealing and
        # approve/disburse their own reward (MONEY-1).
        "other",
    }
)
_DIRECTIONS = {LedgerEntry.Direction.IN, LedgerEntry.Direction.OUT}


def _approval_service() -> IApprovalService:
    return container.resolve(IApprovalService)  # type: ignore[type-abstract]


def _ledger_service() -> ILedgerService:
    return container.resolve(ILedgerService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require(data: dict[str, Any], name: str) -> Any:
    if name not in data or data[name] is None:
        raise _reject(name, "This field is required.")
    return data[name]


def _str_required(raw: Any, name: str, *, max_length: int) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    if "\x00" in raw:
        # psycopg cannot store NUL bytes (mirrors core.http.str_field's guard).
        raise _reject(name, "Null characters are not allowed.")
    value = raw.strip()
    if not value:
        raise _reject(name, "This field may not be blank.")
    if len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _choice(raw: Any, name: str, choices) -> str:
    if not isinstance(raw, str) or raw not in choices:
        raise _reject(name, f"Must be one of: {', '.join(sorted(choices))}.")
    return raw


# --- approval requests -----------------------------------------------------


def _roles(request: HttpRequest) -> set[str]:
    return get_user_roles(request)


def _create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    amount = decimal_field(data, "amount_uzs", max_digits=18, decimal_places=2)
    if amount is not None and amount < Decimal("0.01"):
        raise _reject("amount_uzs", "Ensure this value is greater than or equal to 0.01.")
    return {
        "kind": _choice(_require(data, "kind"), "kind", _REQUEST_KINDS),
        "title": _str_required(_require(data, "title"), "title", max_length=200),
        # The model field is an unbounded TextField — no max_length (matches the old
        # serializer, which had none); str_field still NUL-guards it.
        "description": str_field(data, "description"),
        "amount_uzs": amount,
        "branch": None if data.get("branch") is None else int_field(data, "branch"),
        # A non-dict payload is rejected by create_request with code="payload_invalid"
        # (its own kind-agnostic guard) — pass it through so that exact contract holds.
        "payload": data.get("payload", {}),
    }


@csrf_exempt
@require_auth
def approval_requests_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "approvals:read")
        qs = apply_filters(
            request,
            _approval_service().scoped(user=request.user, roles=_roles(request)),
            filter_fields=("kind", "status", "branch"),
            ordering_fields=("created_at", "amount_uzs"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([approval_request_to_dict(r) for r in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "approvals:write")
        req = _approval_service().create(data=_create_data(request), requested_by=request.user)
        return created(approval_request_to_dict(req))
    return _method_not_allowed()


def _get_request_in_scope(request: HttpRequest, pk: int):
    req = _approval_service().get_scoped(pk=pk, user=request.user, roles=_roles(request))
    if req is None:
        raise NotFoundException(code="not_found")
    return req


@csrf_exempt
@require_auth
def approval_request_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "approvals:read")
    return success(approval_request_to_dict(_get_request_in_scope(request, pk)))


def _decision_note(request: HttpRequest) -> str:
    return str_field(read_json(request), "note", max_length=255)


@csrf_exempt
@require_auth
def approval_request_approve_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "approvals:approve")
    req = _get_request_in_scope(request, pk)
    result = _approval_service().approve(request_id=req.pk, actor=request.user, note=_decision_note(request))
    return success(approval_request_to_dict(result))


@csrf_exempt
@require_auth
def approval_request_reject_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "approvals:approve")
    req = _get_request_in_scope(request, pk)
    result = _approval_service().reject(request_id=req.pk, actor=request.user, note=_decision_note(request))
    return success(approval_request_to_dict(result))


@csrf_exempt
@require_auth
def approval_request_cancel_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "approvals:write")
    req = _get_request_in_scope(request, pk)
    # Only the requester may cancel their own request (approvals:write is broad).
    if not request.user.is_superuser and req.requested_by_id != request.user.id:
        raise PermissionException("You can only cancel your own request.", code="not_requester")
    result = _approval_service().cancel(request_id=req.pk, actor=request.user)
    return success(approval_request_to_dict(result))


@csrf_exempt
@require_auth
def approval_request_disburse_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "approvals:disburse")
    req = _get_request_in_scope(request, pk)
    data = read_json(request)
    result = _approval_service().disburse(
        request_id=req.pk,
        payment_method_id=int_field(data, "payment_method", required=True),  # type: ignore[arg-type]
        actor=request.user,
        direction=_choice(data.get("direction", LedgerEntry.Direction.OUT), "direction", _DIRECTIONS),
        entry_type=str_field(data, "entry_type", max_length=32),
        party_label=str_field(data, "party_label", max_length=200),
    )
    return success(approval_request_to_dict(result))


# --- ledger (read-only) ----------------------------------------------------


@csrf_exempt
@require_auth
def ledger_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "ledger:read")
    assert_tenant_context()
    qs = apply_filters(
        request,
        _ledger_service().list_entries(),
        filter_fields=("direction", "entry_type", "branch", "source_kind"),
        ordering_fields=("created_at", "amount_uzs"),
        default_ordering="-created_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([ledger_entry_to_dict(e) for e in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def ledger_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "ledger:read")
    assert_tenant_context()
    entry = _ledger_service().list_entries().filter(pk=pk).first()
    if entry is None:
        raise NotFoundException(code="not_found")
    return success(ledger_entry_to_dict(entry))
