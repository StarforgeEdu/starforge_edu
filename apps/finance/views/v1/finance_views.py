"""Finance HTTP views (layered, off DRF).

Fee schedules + payment methods (CRUD), invoices (issue via the service /
void / payment-plan, scoped reads), discounts (read-only over CRUD; granted via
Approvals, ended via deactivate), the expense lifecycle, cashier shifts
(open/close/report), the parent-scoped outstanding balance, and the async
statement request/result. Money logic lives in the preserved
apps.finance.services domain fns behind IFinanceService.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt

from apps.cohorts.models import Cohort
from apps.finance.dto import StatementExportRequestDTO
from apps.finance.interfaces.services import IFinanceService
from apps.finance.models import FeeSchedule, InvoiceLine
from apps.finance.openapi_contracts import (
    STATEMENT_REQUEST_OPERATION,
    STATEMENT_RESULT_OPERATIONS,
)
from apps.finance.presenters import (
    cashier_shift_to_dict,
    discount_to_dict,
    expense_to_dict,
    fee_schedule_to_dict,
    invoice_list_to_dict,
    invoice_to_dict,
    outstanding_to_dict,
    payment_method_to_dict,
    payment_plan_to_dict,
    refund_to_dict,
    statement_export_to_dict,
)
from apps.finance.selectors import has_natural_finance_scope
from apps.org.models import Branch
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import (
    NotFoundException,
    PermissionException,
    ServiceUnavailableException,
    ValidationException,
)
from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES
from core.http import decimal_field, int_field, read_json, reject_unknown_fields, str_field
from core.listing import (
    apply_date_range_filters,
    apply_filters,
    paginate,
    parse_date_range_filters,
    positive_int_filter,
)
from core.openapi_contracts import openapi_contract
from core.permissions import PermissionRoleSet, Role, get_user_roles, has_permission_code
from core.responses import created, error, no_content, paginated, success
from core.role_principals import request_role_principal
from core.scoping import (
    assert_permission_membership_scope,
    assert_permission_organization_scope,
    is_permission_unscoped,
    permission_membership_scope_q,
    permission_membership_scopes,
    request_permission_membership_allows,
    scope_to_permission_memberships,
)
from core.utils import stable_hash

_BILLING_PERIODS = frozenset(c[0] for c in FeeSchedule.BillingPeriod.choices)
_LINE_TYPES = frozenset(c[0] for c in InvoiceLine.LineType.choices)
_LOCALES = frozenset({"uz", "ru", "en"})
_MAX_INVOICE_LINES = 500


def _service() -> IFinanceService:
    return container.resolve(IFinanceService)  # type: ignore[type-abstract]


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
        raise _reject(name, "Null characters are not allowed.")
    value = raw.strip()
    if not value:
        raise _reject(name, "This field may not be blank.")
    if len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _choice(raw: Any, name: str, choices: frozenset[str]) -> str:
    if not isinstance(raw, str) or raw not in choices:
        raise _reject(name, f"Must be one of: {', '.join(sorted(choices))}.")
    return raw


def _money(
    data: dict[str, Any], name: str, *, required: bool = True, min_value: Decimal | None = None
) -> Any:
    value = decimal_field(data, name, max_digits=18, decimal_places=2)
    if value is None:
        if required:
            raise _reject(name, "This field is required.")
        return None
    if min_value is not None and value < min_value:
        raise _reject(name, f"Ensure this value is greater than or equal to {min_value}.")
    return value


def _quantity(item: dict[str, Any]) -> Decimal:
    """Invoice-line quantity — DecimalField(max_digits=8, decimal_places=2), default 1.
    The default applies ONLY when the key is ABSENT (an explicit 0 is a real 0-qty
    line, not defaulted to 1); validated at the column's 8 digits so a huge quantity
    is a clean 400, not a decimal-context overflow -> 500 in the amount quantize."""
    if "quantity" not in item:
        return Decimal("1")
    value = decimal_field(item, "quantity", max_digits=8, decimal_places=2)
    if value is None:  # explicit null — the old DecimalField (no allow_null) rejected it.
        raise _reject("quantity", "This field may not be null.")
    if value < 0:
        raise _reject("quantity", "Ensure this value is greater than or equal to 0.")
    return value


def _int_required(data: dict[str, Any], name: str) -> int:
    value = int_field(data, name, required=True)
    if value is None:
        raise _reject(name, "This field is required.")
    return value


def _positive_int(data: dict[str, Any], name: str) -> int | None:
    if name not in data or data[name] is None:
        return None
    raw = data[name]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _reject(name, "Must be an integer.")
    if raw < 0:
        raise _reject(name, "Must be a non-negative integer.")
    return raw


def _bool(data: dict[str, Any], name: str) -> bool:
    raw = data[name]
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.lower() in ("true", "1", "yes", "y", "t", "on"):
        return True
    if isinstance(raw, str) and raw.lower() in ("false", "0", "no", "n", "f", "off"):
        return False
    raise _reject(name, "Must be a boolean.")


def _resolve_cohort(request: HttpRequest, raw: Any):
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _reject("cohort", "Must be an integer id.")
    obj = Cohort.objects.filter(pk=raw).first()
    if obj is None:
        raise _reject("cohort", "Invalid cohort.")
    assert_permission_membership_scope(
        request,
        permission="finance:write",
        branch_id=obj.branch_id,
        department_id=obj.department_id,
        account_kinds={"staff"},
    )
    return obj


def _roles(request: HttpRequest) -> set[str]:
    return get_user_roles(request)


def _scope_cashier_shifts(request: HttpRequest, queryset: QuerySet, *, permission: str) -> QuerySet:
    """Apply exact permission and per-assignment cashier ownership together.

    A cashier assignment may expose only that user's shifts in its own branch.
    A separate support/accounting assignment can expose all shifts only inside
    the branches where *that same assignment* grants the requested permission.
    """
    if request.user.is_superuser:
        return queryset
    roles = _roles(request)
    if not isinstance(roles, PermissionRoleSet):
        # Retain the legacy role-only behavior for explicit compatibility
        # callers. Authenticated request paths normally always use the richer
        # PermissionRoleSet produced by ``get_user_roles``.
        scoped = scope_to_permission_memberships(
            request,
            queryset,
            permission=permission,
            branch_field="branch_id",
            account_kinds={"staff"},
        )
        if Role.CASHIER in roles and not ({Role.DIRECTOR, Role.ACCOUNTANT} & roles):
            return scoped.filter(cashier=request.user)
        return scoped

    visible = Q(pk__in=[])
    for membership in permission_membership_scopes(
        roles=roles,
        permission=permission,
        account_kinds={"staff"},
    ):
        member_scope = (
            Q(pk__isnull=False) if membership.is_organization_wide else Q(branch_id=membership.branch_id)
        )
        if membership.role == Role.CASHIER:
            member_scope &= Q(cashier=request.user)
        visible |= member_scope
    return queryset.filter(visible)


def _student_department_id(student: Any) -> int | None:
    cohort = student.current_cohort
    return cohort.department_id if cohort is not None else None


def _permission_scope_pairs(
    request: HttpRequest,
    *,
    permission: str,
) -> set[tuple[int, int | None]] | None:
    """Exact branch/department boundaries supplying ``permission``.

    ``None`` is the explicit organization-wide/superuser authority marker; an
    empty set is a valid fail-closed result for a scoped caller.
    """
    if is_permission_unscoped(
        request,
        permission=permission,
        account_kinds={"staff"},
    ):
        return None
    return {
        (membership.branch_id, membership.department_id)
        for membership in permission_membership_scopes(
            roles=_roles(request),
            permission=permission,
            account_kinds={"staff"},
        )
    }


def _apply_register_filters(
    request: HttpRequest,
    queryset: QuerySet,
    *,
    branch_field: str,
    date_field: str,
    datetime_field: bool = False,
) -> QuerySet:
    """Apply CEO register filters only after the endpoint's authorization scope."""
    branch_id = positive_int_filter(request, "branch")
    if branch_id is not None:
        queryset = queryset.filter(**{branch_field: branch_id})
    date_from, date_to = parse_date_range_filters(request)
    return apply_date_range_filters(
        queryset,
        field=date_field,
        date_from=date_from,
        date_to=date_to,
        datetime_field=datetime_field,
    )


# --- fee schedules (CRUD) --------------------------------------------------


def _fee_data(request: HttpRequest, *, require_required: bool) -> dict[str, Any]:
    data = read_json(request)
    out: dict[str, Any] = {}
    if require_required or "name" in data:
        out["name"] = _str_required(_require(data, "name"), "name", max_length=120)
    if require_required or "amount_uzs" in data:
        out["amount_uzs"] = _money(data, "amount_uzs", min_value=Decimal("0"))
    if "cohort" in data:
        out["cohort"] = _resolve_cohort(request, data["cohort"])
        if out["cohort"] is None:
            assert_permission_organization_scope(
                request,
                permission="finance:write",
                account_kinds={"staff"},
            )
    if "billing_period" in data:
        out["billing_period"] = _choice(data["billing_period"], "billing_period", _BILLING_PERIODS)
    if "due_day_of_month" in data:
        day = _positive_int(data, "due_day_of_month")
        if day is None:
            raise _reject("due_day_of_month", "This field may not be null.")
        if not 1 <= day <= 31:
            # 0 (or >31) is storable in the PositiveSmallIntegerField but makes
            # _due_date build date(year, month, 0) -> ValueError -> 500 on every issue.
            raise _reject("due_day_of_month", "Must be between 1 and 31.")
        out["due_day_of_month"] = day
    if "is_active" in data:
        out["is_active"] = _bool(data, "is_active")
    return out


@csrf_exempt
@require_auth
def fee_schedules_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "finance:read")
        schedules = _service().fee_schedules()
        if not is_permission_unscoped(
            request,
            permission="finance:read",
            account_kinds={"staff"},
        ):
            schedules = schedules.filter(
                Q(cohort__isnull=True)
                | permission_membership_scope_q(
                    roles=_roles(request),
                    permission="finance:read",
                    branch_field="cohort__branch_id",
                    department_field="cohort__department_id",
                    account_kinds={"staff"},
                )
            )
        qs = apply_filters(
            request,
            schedules,
            filter_fields=("is_active", "cohort", "billing_period"),
            search_fields=("name",),
            ordering_fields=("name", "amount_uzs", "created_at"),
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([fee_schedule_to_dict(f) for f in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "finance:write")
        fee_data = _fee_data(request, require_required=True)
        if "cohort" not in fee_data:
            assert_permission_organization_scope(
                request,
                permission="finance:write",
                account_kinds={"staff"},
            )
        fs = _service().create_fee_schedule(data=fee_data)
        return created(fee_schedule_to_dict(fs))
    return _method_not_allowed()


def _get_fee_schedule(request: HttpRequest, pk: int, *, permission: str) -> FeeSchedule:
    fs = _service().fee_schedule(pk)
    if fs is None:
        raise NotFoundException(code="not_found")
    cohort = fs.cohort
    if cohort is not None:
        assert_permission_membership_scope(
            request,
            permission=permission,
            branch_id=cohort.branch_id,
            department_id=cohort.department_id,
            account_kinds={"staff"},
        )
    elif permission == "finance:write":
        assert_permission_organization_scope(
            request,
            permission=permission,
            account_kinds={"staff"},
        )
    return fs


@csrf_exempt
@require_auth
def fee_schedule_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "finance:read")
        return success(fee_schedule_to_dict(_get_fee_schedule(request, pk, permission="finance:read")))
    if request.method in ("PUT", "PATCH"):
        check_perm(request, "finance:write")
        fs = _get_fee_schedule(request, pk, permission="finance:write")
        # PUT requires the required fields; PATCH is partial.
        changes = _fee_data(request, require_required=(request.method == "PUT"))
        fs = _service().update_fee_schedule(fee_schedule=fs, changes=changes)
        return success(fee_schedule_to_dict(fs))
    if request.method == "DELETE":
        check_perm(request, "finance:write")
        _service().delete_fee_schedule(
            fee_schedule=_get_fee_schedule(request, pk, permission="finance:write")
        )
        return no_content()
    return _method_not_allowed()


# --- invoices --------------------------------------------------------------


def _invoice_lines(data: dict[str, Any]) -> list[dict] | None:
    if "lines" not in data or data["lines"] is None:
        return None
    raw_lines = data["lines"]
    if not isinstance(raw_lines, list):
        raise _reject("lines", "lines must be a list of line objects.")
    if len(raw_lines) > _MAX_INVOICE_LINES:
        raise _reject("lines", f"lines may contain at most {_MAX_INVOICE_LINES} items.")
    out: list[dict] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            raise _reject("lines", "each line must be an object.")
        line_type = _choice(item.get("line_type", InvoiceLine.LineType.OTHER), "line_type", _LINE_TYPES)
        unit_price = _money(
            item,
            "unit_price_uzs",
            min_value=None if line_type == InvoiceLine.LineType.DISCOUNT else Decimal("0"),
        )
        out.append(
            {
                "description": _str_required(_require(item, "description"), "description", max_length=255),
                "line_type": line_type,
                "quantity": _quantity(item),
                "unit_price_uzs": unit_price,
            }
        )
    return out


@csrf_exempt
@require_auth
def invoices_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "finance:read")
        invoices = _apply_register_filters(
            request,
            _service().invoices(user=request.user, roles=_roles(request)),
            branch_field="branch_at_issue_id",
            date_field="issue_date",
        )
        qs = apply_filters(
            request,
            invoices,
            filter_fields=("status", "student", "cohort", "fee_schedule"),
            search_fields=("number",),
            ordering_fields=("created_at", "due_date", "total_uzs"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([invoice_list_to_dict(i) for i in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "finance:write")
        data = read_json(request)
        student_id = int_field(data, "student", required=True)
        fee_schedule_id = int_field(data, "fee_schedule")
        if student_id is not None:
            from apps.students.models import StudentProfile

            student_scope = (
                StudentProfile.objects.filter(pk=student_id)
                .values_list("branch_id", "current_cohort__department_id")
                .first()
            )
            if student_scope is not None:
                assert_permission_membership_scope(
                    request,
                    permission="finance:write",
                    branch_id=student_scope[0],
                    department_id=student_scope[1],
                    account_kinds={"staff"},
                )
        if fee_schedule_id is not None:
            schedule = FeeSchedule.objects.select_related("cohort").filter(pk=fee_schedule_id).first()
            schedule_cohort = schedule.cohort if schedule is not None else None
            if schedule_cohort is not None:
                assert_permission_membership_scope(
                    request,
                    permission="finance:write",
                    branch_id=schedule_cohort.branch_id,
                    department_id=schedule_cohort.department_id,
                    account_kinds={"staff"},
                )
        period = str_field(data, "period", max_length=16)
        invoice = _service().issue_invoice(
            student_id=student_id,  # type: ignore[arg-type]
            fee_schedule_id=fee_schedule_id,
            lines=_invoice_lines(data),
            period=period,
            created_by=request.user,
            allowed_scope_pairs=_permission_scope_pairs(
                request,
                permission="finance:write",
            ),
        )
        fresh = _service().reload_invoice(
            pk=invoice.pk,
            user=request.user,
            roles=_roles(request),
            permission="finance:write",
        )
        return created(invoice_to_dict(fresh or invoice))
    return _method_not_allowed()


def _get_invoice(request: HttpRequest, pk: int, *, permission: str = "finance:read"):
    inv = _service().invoice(
        pk=pk,
        user=request.user,
        roles=_roles(request),
        permission=permission,
    )
    if inv is None:
        raise NotFoundException(code="not_found")
    if permission != "finance:read":
        assert_permission_membership_scope(
            request,
            permission=permission,
            branch_id=inv.branch_at_issue_id,
            department_id=inv.department_at_issue_id,
            account_kinds={"staff"},
        )
    return inv


@csrf_exempt
@require_auth
def invoice_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "finance:read")
    return success(invoice_to_dict(_get_invoice(request, pk)))


@csrf_exempt
@require_auth
def invoice_void_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "finance:write")
    invoice = _get_invoice(request, pk, permission="finance:write")
    _service().void_invoice(invoice=invoice, actor=request.user)
    fresh = _service().reload_invoice(
        pk=invoice.pk,
        user=request.user,
        roles=_roles(request),
        permission="finance:write",
    )
    return success(invoice_to_dict(fresh or invoice))


def _installments(data: dict[str, Any]) -> list[dict]:
    raw = _require(data, "installments")
    if not isinstance(raw, list) or not raw:
        raise _reject("installments", "installments must be a non-empty list.")
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _reject("installments", "each installment must be an object.")
        raw_due = item.get("due_date")
        if not isinstance(raw_due, str) or not raw_due.strip():
            raise _reject("installments", "each installment needs a due_date.")
        try:
            due = date.fromisoformat(raw_due.strip())
        except ValueError as exc:
            raise _reject("installments", "each due_date must be YYYY-MM-DD.") from exc
        out.append(
            {
                "due_date": due,
                "amount_uzs": _money(
                    item,
                    "amount_uzs",
                    min_value=Decimal("0.01"),
                ),
            }
        )
    return out


@csrf_exempt
@require_auth
def invoice_payment_plan_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "finance:write")
    invoice = _get_invoice(request, pk, permission="finance:write")
    plan = _service().create_payment_plan(
        invoice=invoice, installments=_installments(read_json(request)), created_by=request.user
    )
    return created(payment_plan_to_dict(plan))


# --- discounts (read-only over CRUD; deactivate) ---------------------------


@csrf_exempt
@require_auth
def discounts_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "finance:read")
        qs = apply_filters(
            request,
            scope_to_permission_memberships(
                request,
                _service().discounts(),
                permission="finance:read",
                branch_field="student__branch_id",
                department_field="student__current_cohort__department_id",
                account_kinds={"staff"},
            ),
            filter_fields=("student", "discount_type", "is_active"),
            ordering_fields=("created_at",),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([discount_to_dict(d) for d in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        # Discounts are GRANTED through an approval request, never created directly.
        return error(
            "Discounts are granted through an approval request, not created directly.",
            code="method_not_allowed",
            status=405,
        )
    return _method_not_allowed()


@csrf_exempt
@require_auth
def discount_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "finance:read")
    d = _service().discount(pk)
    if d is None:
        raise NotFoundException(code="not_found")
    assert_permission_membership_scope(
        request,
        permission="finance:read",
        branch_id=d.student.branch_id,
        department_id=_student_department_id(d.student),
        account_kinds={"staff"},
    )
    return success(discount_to_dict(d))


@csrf_exempt
@require_auth
def discount_deactivate_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "finance:write")
    d = _service().discount(pk)
    if d is None:
        raise NotFoundException(code="not_found")
    assert_permission_membership_scope(
        request,
        permission="finance:write",
        branch_id=d.student.branch_id,
        department_id=_student_department_id(d.student),
        account_kinds={"staff"},
    )
    d = _service().deactivate_discount(discount=d)
    return success(discount_to_dict(d))


# --- payment methods (CRUD) ------------------------------------------------


_PAYMENT_METHOD_SLUG_RE = re.compile(r"[-a-zA-Z0-9_]+\Z")


def _payment_method_slug(raw: Any) -> str:
    value = _str_required(raw, "slug", max_length=64)
    if _PAYMENT_METHOD_SLUG_RE.fullmatch(value) is None:
        raise _reject("slug", "Use only letters, numbers, hyphens, and underscores.")
    return value


def _payment_method_data(
    request: HttpRequest,
    *,
    require_required: bool,
    generate_slug: bool = False,
) -> dict[str, Any]:
    data = read_json(request)
    out: dict[str, Any] = {}
    if require_required or "name" in data:
        out["name"] = _str_required(_require(data, "name"), "name", max_length=64)
    if "slug" in data:
        out["slug"] = _payment_method_slug(data["slug"])
    elif generate_slug and "name" in out:
        generated = slugify(out["name"])[:64]
        # ``slugify`` strips non-Latin alphabets. Fall back to a stable ASCII
        # identifier so distinct Cyrillic/Uzbek names never collide on ``""``.
        out["slug"] = generated or f"method-{stable_hash(out['name'])[:12]}"
    if "is_active" in data:
        out["is_active"] = _bool(data, "is_active")
    return out


@csrf_exempt
@require_auth
def payment_methods_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "finance:read")
        qs = apply_filters(
            request,
            _service().payment_methods(),
            filter_fields=("is_active",),
            search_fields=("name", "slug"),
            ordering_fields=("name",),
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([payment_method_to_dict(m) for m in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "finance:write")
        assert_permission_organization_scope(
            request,
            permission="finance:write",
            account_kinds={"staff"},
        )
        pm = _service().create_payment_method(
            data=_payment_method_data(request, require_required=True, generate_slug=True)
        )
        return created(payment_method_to_dict(pm))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def payment_method_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "finance:read")
        pm = _service().payment_method(pk)
        if pm is None:
            raise NotFoundException(code="not_found")
        return success(payment_method_to_dict(pm))
    if request.method in ("PUT", "PATCH"):
        check_perm(request, "finance:write")
        assert_permission_organization_scope(
            request,
            permission="finance:write",
            account_kinds={"staff"},
        )
        pm = _service().payment_method(pk)
        if pm is None:
            raise NotFoundException(code="not_found")
        changes = _payment_method_data(request, require_required=(request.method == "PUT"))
        pm = _service().update_payment_method(payment_method=pm, changes=changes)
        return success(payment_method_to_dict(pm))
    if request.method == "DELETE":
        check_perm(request, "finance:write")
        assert_permission_organization_scope(
            request,
            permission="finance:write",
            account_kinds={"staff"},
        )
        pm = _service().payment_method(pk)
        if pm is None:
            raise NotFoundException(code="not_found")
        _service().delete_payment_method(payment_method=pm)
        return no_content()
    return _method_not_allowed()


# --- expenses --------------------------------------------------------------


@csrf_exempt
@require_auth
def expenses_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "finance:read")
        expenses = scope_to_permission_memberships(
            request,
            _service().expenses(),
            permission="finance:read",
            branch_field="branch_id",
            account_kinds={"staff"},
        )
        expenses = _apply_register_filters(
            request,
            expenses,
            branch_field="branch_id",
            date_field="created_at",
            datetime_field=True,
        )
        qs = apply_filters(
            request,
            expenses,
            filter_fields=("status", "category"),
            ordering_fields=("created_at", "amount_uzs"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([expense_to_dict(e) for e in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "approvals:write")
        data = read_json(request)
        branch = Branch.objects.filter(pk=_int_required(data, "branch"), archived_at__isnull=True).first()
        if branch is None:
            raise _reject("branch", "Invalid branch.")
        assert_permission_membership_scope(
            request,
            permission="approvals:write",
            branch_id=branch.pk,
            enforce_department=False,
            account_kinds={"staff", "teacher"},
        )
        expense = _service().create_expense(
            branch=branch,
            description=_str_required(_require(data, "description"), "description", max_length=255),
            amount_uzs=_money(data, "amount_uzs", min_value=Decimal("0.01")),
            category=str_field(data, "category", max_length=80),
            created_by=request.user,
        )
        return created(expense_to_dict(expense))
    return _method_not_allowed()


def _get_expense(request: HttpRequest, pk: int, *, permission: str):
    e = _service().expense(pk)
    if e is None:
        raise NotFoundException(code="not_found")
    assert_permission_membership_scope(
        request,
        permission=permission,
        branch_id=e.branch_id,
        enforce_department=False,
        account_kinds={"staff"},
    )
    return e


@csrf_exempt
@require_auth
def expense_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "finance:read")
    return success(expense_to_dict(_get_expense(request, pk, permission="finance:read")))


@csrf_exempt
@require_auth
def expense_approve_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "approvals:approve")
    expense = _service().approve_expense(
        expense_id=_get_expense(request, pk, permission="approvals:approve").pk,
        actor=request.user,
    )
    return success(expense_to_dict(expense))


@csrf_exempt
@require_auth
def expense_reject_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "approvals:approve")
    expense = _get_expense(request, pk, permission="approvals:approve")
    reason = str_field(read_json(request), "reason", max_length=255)
    expense = _service().reject_expense(expense_id=expense.pk, reason=reason, actor=request.user)
    return success(expense_to_dict(expense))


@csrf_exempt
@require_auth
def expense_pay_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "approvals:disburse")
    expense = _get_expense(request, pk, permission="approvals:disburse")
    pm_id = int_field(read_json(request), "payment_method", required=True)
    expense = _service().pay_expense(expense_id=expense.pk, payment_method_id=pm_id, actor=request.user)  # type: ignore[arg-type]
    return success(expense_to_dict(expense))


# --- refunds ---------------------------------------------------------------


def _get_refund(request: HttpRequest, pk: int, *, permission: str):
    refund = _service().refund(pk)
    if (
        refund is None
        or refund.invoice.attribution_status not in ATTRIBUTED_SCOPE_STATUSES
        or refund.invoice.branch_at_issue_id is None
    ):
        raise NotFoundException(code="not_found")
    assert_permission_membership_scope(
        request,
        permission=permission,
        branch_id=refund.invoice.branch_at_issue_id,
        department_id=refund.invoice.department_at_issue_id,
        account_kinds={"staff"},
    )
    return refund


@csrf_exempt
@require_auth
def refunds_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "finance:read")
    qs = scope_to_permission_memberships(
        request,
        _service().refunds().filter(invoice__attribution_status__in=ATTRIBUTED_SCOPE_STATUSES),
        permission="finance:read",
        branch_field="invoice__branch_at_issue_id",
        department_field="invoice__department_at_issue_id",
        account_kinds={"staff"},
    )
    qs = _apply_register_filters(
        request,
        qs,
        branch_field="invoice__branch_at_issue_id",
        date_field="created_at",
        datetime_field=True,
    )
    qs = apply_filters(
        request,
        qs,
        filter_fields=("state", "provider", "invoice", "payment_id"),
        ordering_fields=("created_at", "amount_uzs"),
        default_ordering="-created_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated(
        [refund_to_dict(refund) for refund in items],
        total=total,
        page=page,
        page_size=size,
    )


@csrf_exempt
@require_auth
def refund_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "finance:read")
    return success(refund_to_dict(_get_refund(request, pk, permission="finance:read")))


@csrf_exempt
@require_auth
def refund_approve_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "approvals:approve")
    refund = _get_refund(request, pk, permission="approvals:approve")
    refund = _service().approve_refund(refund_id=refund.pk, actor=request.user)
    return success(refund_to_dict(refund))


# --- cashier shifts --------------------------------------------------------


@csrf_exempt
@require_auth
def cashier_shifts_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "finance:read")
        shifts = _scope_cashier_shifts(
            request,
            _service().cashier_shifts(),
            permission="finance:read",
        )
        qs = apply_filters(
            request,
            shifts,
            filter_fields=("status", "cashier", "branch"),
            ordering_fields=("opened_at", "closed_at"),
            default_ordering="-opened_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([cashier_shift_to_dict(s) for s in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        # Shifts are opened via /cashier-shifts/open/, never a raw collection-create.
        return error("Open a shift via /finance/cashier-shifts/open/.", code="method_not_allowed", status=405)
    return _method_not_allowed()


@csrf_exempt
@require_auth
def cashier_shift_open_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "payments:write")
    data = read_json(request)
    branch = Branch.objects.filter(
        pk=_int_required(data, "branch"),
        archived_at__isnull=True,
    ).first()
    if branch is None:
        raise _reject("branch", "Invalid branch.")
    assert_permission_membership_scope(
        request,
        permission="payments:write",
        branch_id=branch.pk,
        enforce_department=False,
        account_kinds={"staff"},
    )
    shift = _service().open_cashier_shift(
        cashier=request.user,
        branch=branch,
        opening_cash_uzs=_money(data, "opening_cash_uzs", required=False) or Decimal("0"),
        notes=str_field(data, "notes"),
    )
    return created(cashier_shift_to_dict(shift))


def _get_shift(request: HttpRequest, pk: int, *, permission: str):
    s = (
        _scope_cashier_shifts(
            request,
            _service().cashier_shifts(),
            permission=permission,
        )
        .filter(pk=pk)
        .first()
    )
    if s is None:
        raise NotFoundException(code="not_found")
    return s


@csrf_exempt
@require_auth
def cashier_shift_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "finance:read")
    return success(cashier_shift_to_dict(_get_shift(request, pk, permission="finance:read")))


@csrf_exempt
@require_auth
def cashier_shift_close_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "payments:write")
    shift = _get_shift(request, pk, permission="payments:write")
    data = read_json(request)
    shift = _service().close_cashier_shift(
        shift=shift,
        closing_cash_uzs=_money(data, "closing_cash_uzs"),
        notes=str_field(data, "notes"),
        actor=request.user,
    )
    return success(cashier_shift_to_dict(shift))


@csrf_exempt
@require_auth
def cashier_shift_report_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "finance:read")
    return success(_service().cashier_shift_report(shift=_get_shift(request, pk, permission="finance:read")))


# --- outstanding balance (parent-scoped) -----------------------------------


def _require_int_param(request: HttpRequest, name: str) -> int:
    raw = request.GET.get(name)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationException(
            f"Query parameter '{name}' is required and must be an integer.",
            code="invalid_query_param",
            fields={name: ["This query parameter is required."]},
        ) from None


def _can_view_balance(*, user, student_id: int, roles: set[str]) -> bool:
    if has_natural_finance_scope(
        roles,
        account_kind="parent",
        legacy_role=Role.PARENT,
    ) and _service().parent_can_see_student(user=user, student_id=student_id):
        return True
    if has_natural_finance_scope(
        roles,
        account_kind="student",
        legacy_role=Role.STUDENT,
    ):
        from apps.students.models import StudentProfile

        return StudentProfile.objects.filter(pk=student_id, user=user).exists()
    return False


@csrf_exempt
@require_auth
def outstanding_balance_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    user: Any = request.user
    roles = _roles(request)
    # Admit finance:read (staff) OR finance:read_own (parent/student); anyone else
    # is denied (mirrors the old FinanceBalanceReadPermission fail-closed gate).
    if not (
        user.is_superuser
        or has_permission_code(roles, "finance:read")
        or has_permission_code(roles, "finance:read_own")
    ):
        raise PermissionException("Insufficient finance access.", code="forbidden")
    student_id = _require_int_param(request, "student")
    from apps.students.models import StudentProfile

    student = StudentProfile.objects.select_related("current_cohort").filter(pk=student_id).first()
    if student is None:
        raise NotFoundException(code="not_found")
    is_staff = user.is_superuser or has_permission_code(roles, "finance:read")
    if is_staff:
        if not is_permission_unscoped(
            request,
            permission="finance:read",
            account_kinds={"staff"},
        ):
            current_scope_visible = request_permission_membership_allows(
                request,
                permission="finance:read",
                branch_id=student.branch_id,
                department_id=_student_department_id(student),
                account_kinds={"staff"},
            )
            historical_scope_visible = (
                _service().invoices(user=user, roles=roles).filter(student_id=student_id).exists()
            )
            if not current_scope_visible and not historical_scope_visible:
                raise NotFoundException(code="not_found")
    else:
        has_natural_scope = has_natural_finance_scope(
            roles,
            account_kind="parent",
            legacy_role=Role.PARENT,
        ) or has_natural_finance_scope(
            roles,
            account_kind="student",
            legacy_role=Role.STUDENT,
        )
        if has_natural_scope:
            if not _can_view_balance(user=user, student_id=student_id, roles=roles):
                raise NotFoundException(code="not_found")
        else:
            raise PermissionException("Insufficient finance access.", code="forbidden")
    balance, invoices = _service().outstanding(student_id=student_id, user=user, roles=roles)
    return success(outstanding_to_dict(student_id=student_id, outstanding_uzs=balance, invoices=invoices))


# --- statements (async) ----------------------------------------------------


@openapi_contract(
    path="/api/v1/finance/students/{student_id}/statement/",
    operations=(STATEMENT_REQUEST_OPERATION,),
)
@csrf_exempt
@require_auth
def statement_request_view(request: HttpRequest, student_id: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "finance:read")
    from core.ratelimit import check_rate
    from core.utils import current_schema

    # Per-request cap (mirrors the other expensive async enqueues — AI generation,
    # bulk-import, announcements): each POST spawns an unbounded WeasyPrint render + a
    # fresh S3 object with NO budget cap or dedupe, so an unthrottled finance:read
    # holder could flood the shared Celery pool and grow storage without bound.
    check_rate(scope="finance_statement", key=f"{current_schema()}:{request.user.pk}", limit=10, window=60)
    body = read_json(request)
    reject_unknown_fields(body, allowed={"locale"})
    locale = _choice(body.get("locale", "en"), "locale", _LOCALES)
    principal = request_role_principal(request, allowed_kinds={"staff", "teacher"})
    export, should_enqueue = _service().request_statement_export(
        student_id=student_id,
        requested_by=request.user,
        principal=principal,
        dto=StatementExportRequestDTO(locale=locale),
    )
    from celery_tasks.finance_tasks import generate_statement_pdf

    if should_enqueue:
        try:
            generate_statement_pdf.delay(str(export.pk), _schema_name=current_schema())
        except Exception as exc:
            raise ServiceUnavailableException(code="statement_queue_unavailable") from exc
    export = _service().statement_export(export.pk) or export
    url = _service().statement_export_download_url(export)
    return success(statement_export_to_dict(export, url=url), status=202)


@openapi_contract(
    path="/api/v1/finance/statements/{export_id}/",
    operations=STATEMENT_RESULT_OPERATIONS,
)
@csrf_exempt
@require_auth
def statement_result_view(request: HttpRequest, export_id: str) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "finance:read")
    try:
        export_uuid = UUID(export_id)
    except (TypeError, ValueError, AttributeError):
        raise NotFoundException(code="not_found") from None
    principal = request_role_principal(request, allowed_kinds={"staff", "teacher"})
    export = _service().statement_export(export_uuid)
    if (
        export is None
        or export.requested_by_id_snapshot != request.user.pk
        or export.requested_principal_kind != principal.kind
        or export.requested_principal_id != principal.principal_id
        or not _service().statement_export_is_visible(export)
    ):
        raise NotFoundException(code="not_found")
    url = _service().statement_export_download_url(export)
    return success(statement_export_to_dict(export, url=url))
