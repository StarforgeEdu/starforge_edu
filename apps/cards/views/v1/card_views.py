"""Cards endpoints — plain Django views over the layered architecture.

Card types (card:read/write) are centre config. Cards are branch-scoped: a manager
(card:write) issues to their branch's students + revokes; door staff (card:scan) scan a
code to check a student in; a student reads only their OWN card(s). Wallets are stored
value: a student reads their own (/wallets/me/); staff (wallet:read/write) read/top-up/
spend on a branch student's wallet. Money never overdraws / overflows.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.cards.dto.card_dto import WalletAmountDTO
from apps.cards.interfaces.services import ICardService, ICardTypeService, IWalletService
from apps.cards.openapi_contracts import (
    STUDENT_WALLET_CONTRACTS,
    WALLET_ME_CONTRACTS,
    WALLET_REFUND_CONTRACTS,
    WALLET_SPEND_CONTRACTS,
    WALLET_TOPUP_CONTRACTS,
)
from apps.cards.presenters import (
    card_scan_to_dict,
    card_to_dict,
    card_type_to_dict,
    scan_to_dict,
    wallet_payload_to_dict,
    wallet_txn_to_dict,
)
from apps.students.selectors import student_profile_for
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import bool_field, decimal_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.openapi_contracts import openapi_contract
from core.permissions import get_user_roles, has_permission_code
from core.responses import created, error, paginated, success
from core.role_principals import STAFF_PRINCIPAL_KINDS, request_role_principal
from core.scoping import is_permission_unscoped, permission_membership_branch_ids

_MIN_AMOUNT = Decimal("0.01")


def _card_type_service() -> ICardTypeService:
    return container.resolve(ICardTypeService)  # type: ignore[type-abstract]


def _card_service() -> ICardService:
    return container.resolve(ICardService)  # type: ignore[type-abstract]


def _wallet_service() -> IWalletService:
    return container.resolve(IWalletService)  # type: ignore[type-abstract]


def _card_scope(request: HttpRequest, permission: str = "card:read") -> tuple[bool, bool, set[int], Any]:
    """(is_director, is_card_staff, branch_ids, student_profile). Card staff = holds
    card:write OR card:scan (door staff read their branch's cards); a plain student sees
    only their own."""
    req: Any = request
    roles = get_user_roles(req)
    staff_permissions = ("card:write", "card:scan") if permission == "card:read" else (permission,)
    is_card_staff = any(has_permission_code(roles, code) for code in staff_permissions)
    visibility_permissions = {permission, *staff_permissions}
    is_director = any(is_permission_unscoped(req, permission=code) for code in visibility_permissions)
    branch_ids: set[int] = set()
    for code in staff_permissions:
        branch_ids |= permission_membership_branch_ids(roles=roles, permission=code)
    profile = None if (is_director or is_card_staff) else student_profile_for(request.user)
    return is_director, is_card_staff, branch_ids, profile


def _wallet_scope(request: HttpRequest, permission: str) -> tuple[bool, set[int]]:
    req: Any = request
    roles = get_user_roles(req)
    is_director = is_permission_unscoped(req, permission=permission)
    branch_ids = permission_membership_branch_ids(roles=roles, permission=permission)
    return is_director, branch_ids


# --- card types ------------------------------------------------------------
@csrf_exempt
@require_auth
def card_types_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "card:read")
        qs = apply_filters(
            request,
            _card_type_service().list(),
            filter_fields=("is_active",),
            search_fields=("name",),
            ordering_fields=("name", "created_at"),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([card_type_to_dict(ct) for ct in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "card:write")
        body = read_json(request)
        name = str_field(body, "name", max_length=100).strip()
        if not name:
            raise ValidationException(
                "name is required.", code="validation_error", fields={"name": ["This field is required."]}
            )
        card_type = _card_type_service().create(
            name=name, is_active=bool_field(body, "is_active", default=True), created_by=request.user
        )
        return created(card_type_to_dict(card_type))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def card_type_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "card:read" if read else "card:write")
    card_type = _card_type_service().get(pk=pk)
    if card_type is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(card_type_to_dict(card_type))
    if request.method == "PATCH":
        body = read_json(request)
        changes: dict[str, Any] = {}
        if "name" in body:
            name = str_field(body, "name", max_length=100).strip()
            if not name:
                raise ValidationException(
                    "name may not be blank.", code="validation_error", fields={"name": ["May not be blank."]}
                )
            changes["name"] = name
        if "is_active" in body:
            changes["is_active"] = bool_field(body, "is_active")
        return success(card_type_to_dict(_card_type_service().update(card_type, changes)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


# --- cards -----------------------------------------------------------------
@csrf_exempt
@require_auth
def cards_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "card:read")
        is_director, is_card_staff, branch_ids, profile = _card_scope(request)
        qs = _card_service().scoped_list(
            is_director=is_director, is_card_staff=is_card_staff, branch_ids=branch_ids, profile=profile
        )
        qs = apply_filters(
            request,
            qs,
            filter_fields=("student", "card_type", "is_active"),
            ordering_fields=("issued_at",),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([card_to_dict(c) for c in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "card:write")
        return _issue_card(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def card_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "card:read")
    is_director, is_card_staff, branch_ids, profile = _card_scope(request)
    card = _card_service().get_visible(
        is_director=is_director, is_card_staff=is_card_staff, branch_ids=branch_ids, profile=profile, pk=pk
    )
    if card is None:
        raise NotFoundException(code="not_found")
    return success(card_to_dict(card))


@csrf_exempt
@require_auth
def card_revoke_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "card:write")
    is_director, is_card_staff, branch_ids, profile = _card_scope(request, "card:write")
    card = _card_service().get_visible(
        is_director=is_director, is_card_staff=is_card_staff, branch_ids=branch_ids, profile=profile, pk=pk
    )
    if card is None:
        raise NotFoundException(code="not_found")
    body = read_json(request)
    result = _card_service().revoke(
        card, actor=request.user, reason=str_field(body, "reason", max_length=255)
    )
    return success(card_to_dict(result))


@csrf_exempt
@require_auth
def card_scan_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "card:scan")
    body = read_json(request)
    code = str_field(body, "code", max_length=64).strip()
    if not code:
        raise ValidationException(
            "code is required.", code="validation_error", fields={"code": ["This field is required."]}
        )
    note = str_field(body, "note", max_length=255).strip()
    is_director, _is_card_staff, branch_ids, _profile = _card_scope(request, "card:scan")
    return success(
        scan_to_dict(
            _card_service().scan(
                code=code,
                scanned_by=request.user,
                note=note,
                is_unscoped=is_director,
                branch_ids=branch_ids,
            )
        )
    )


@csrf_exempt
@require_auth
def card_scans_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "card:read")
    is_director, is_card_staff, branch_ids, _profile = _card_scope(request)
    # Students hold card:read for their own card, but failed entry attempts and
    # scanner identity form a staff audit trail rather than student-facing data.
    if not is_director and not is_card_staff:
        raise PermissionException(
            _("Scan history is available to card staff only."), code="permission_denied"
        )
    qs = apply_filters(
        request,
        _card_service().scoped_scans(is_director=is_director, branch_ids=branch_ids),
        filter_fields=("card", "was_valid", "scanned_by"),
        ordering_fields=("scanned_at",),
        default_ordering="-scanned_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([card_scan_to_dict(scan) for scan in items], total=total, page=page, page_size=size)


# --- wallets ---------------------------------------------------------------
@openapi_contract(
    path="/api/v1/cards/wallets/me/",
    operations=WALLET_ME_CONTRACTS,
)
@csrf_exempt
@require_auth
def wallet_me_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    profile = student_profile_for(request.user)  # authenticated-only; own wallet
    if profile is None:
        raise NotFoundException(_("You do not have a student profile."), code="not_a_student")
    return success(wallet_payload_to_dict(_wallet_service().wallet_payload(student=profile)))


@openapi_contract(
    path="/api/v1/cards/wallets/{student_id}/",
    operations=STUDENT_WALLET_CONTRACTS,
)
@csrf_exempt
@require_auth
def student_wallet_view(request: HttpRequest, student_id: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "wallet:read")
    student, _is_unscoped, _branch_ids = _student_in_scope(
        request, student_id, "wallet:read"
    )
    return success(wallet_payload_to_dict(_wallet_service().wallet_payload(student=student)))


@openapi_contract(
    path="/api/v1/cards/wallets/{student_id}/topup/",
    operations=WALLET_TOPUP_CONTRACTS,
)
@csrf_exempt
@require_auth
def wallet_topup_view(request: HttpRequest, student_id: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "wallet:write")
    student, is_unscoped, branch_ids = _student_in_scope(request, student_id, "wallet:write")
    txn = _wallet_service().top_up(
        _amount_dto(read_json(request)),
        student=student,
        actor=request.user,
        principal=request_role_principal(request, allowed_kinds=STAFF_PRINCIPAL_KINDS),
        idempotency_key=request.headers.get("Idempotency-Key", ""),
        is_unscoped=is_unscoped,
        branch_ids=branch_ids,
    )
    return created(wallet_txn_to_dict(txn))


@openapi_contract(
    path="/api/v1/cards/wallets/{student_id}/spend/",
    operations=WALLET_SPEND_CONTRACTS,
)
@csrf_exempt
@require_auth
def wallet_spend_view(request: HttpRequest, student_id: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "wallet:write")
    student, is_unscoped, branch_ids = _student_in_scope(request, student_id, "wallet:write")
    txn = _wallet_service().spend(
        _amount_dto(read_json(request)),
        student=student,
        actor=request.user,
        principal=request_role_principal(request, allowed_kinds=STAFF_PRINCIPAL_KINDS),
        idempotency_key=request.headers.get("Idempotency-Key", ""),
        is_unscoped=is_unscoped,
        branch_ids=branch_ids,
    )
    return created(wallet_txn_to_dict(txn))


@openapi_contract(
    path="/api/v1/cards/wallets/{student_id}/refund/",
    operations=WALLET_REFUND_CONTRACTS,
)
@csrf_exempt
@require_auth
def wallet_refund_view(request: HttpRequest, student_id: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "wallet:write")
    student, is_unscoped, branch_ids = _student_in_scope(request, student_id, "wallet:write")
    txn = _wallet_service().refund(
        _amount_dto(read_json(request)),
        student=student,
        actor=request.user,
        principal=request_role_principal(request, allowed_kinds=STAFF_PRINCIPAL_KINDS),
        idempotency_key=request.headers.get("Idempotency-Key", ""),
        is_unscoped=is_unscoped,
        branch_ids=branch_ids,
    )
    return created(wallet_txn_to_dict(txn))


# --- helpers ---------------------------------------------------------------
def _issue_card(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    student = _card_service().resolve_student(student_id=int_field(body, "student", required=True))  # type: ignore[arg-type]
    if student is None:
        raise ValidationException(
            "Unknown student.", code="validation_error", fields={"student": ["No such student."]}
        )
    card_type = _card_service().resolve_active_card_type(
        card_type_id=int_field(body, "card_type", required=True)  # type: ignore[arg-type]
    )
    if card_type is None:  # unknown or retired -> not in the issuable set
        raise ValidationException(
            "Unknown or inactive card type.",
            code="validation_error",
            fields={"card_type": ["No such active card type."]},
        )
    is_director, _is_card_staff, branch_ids, _profile = _card_scope(request, "card:write")
    if not is_director and student.branch_id not in branch_ids:
        raise PermissionException(
            _("You can only issue cards to a student in your own branch."), code="branch_out_of_scope"
        )
    card = _card_service().issue(student=student, card_type=card_type, issued_by=request.user)
    return created(card_to_dict(card))


def _student_in_scope(request: HttpRequest, student_id: int, permission: str):
    is_director, branch_ids = _wallet_scope(request, permission)
    student = _wallet_service().get_student_in_scope(
        student_id=student_id, is_director=is_director, branch_ids=branch_ids
    )
    return student, is_director, branch_ids


def _amount_dto(body: dict[str, Any]) -> WalletAmountDTO:
    unknown = sorted(set(body) - {"amount", "note"})
    if unknown:
        raise ValidationException(
            "Unknown wallet-operation field.",
            code="validation_error",
            fields={field: ["Unknown field."] for field in unknown},
        )
    amount = decimal_field(body, "amount", max_digits=18)
    if amount is None or amount < _MIN_AMOUNT:
        raise ValidationException(
            "amount must be at least 0.01.",
            code="validation_error",
            fields={"amount": ["Must be a number >= 0.01."]},
        )
    return WalletAmountDTO(amount=amount, note=str_field(body, "note", max_length=255))
