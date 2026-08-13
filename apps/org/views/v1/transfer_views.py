"""Branch-transfer endpoints — read-only audit list (D1-LF-6)."""

from __future__ import annotations

from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.org.interfaces.services import IBranchTransferService
from apps.org.models import BranchTransfer
from apps.org.presenters import transfer_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException
from core.http import bool_field, int_field, read_json, reject_unknown_fields, trimmed_str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, paginated, success
from core.scoping import is_permission_unscoped, permission_membership_branch_wide_ids

_RESOURCE = "org"
_FILTERS = ("subject_kind", "subject_id", "student", "from_branch", "to_branch")
_ORDERING = ("created_at",)
_CREATE_FIELDS = frozenset(
    {
        "subject_kind",
        "subject",
        "student",
        "from_branch",
        "to_branch",
        "to_department",
        "reason",
        "confirm_impacts",
    }
)


def _service() -> IBranchTransferService:
    return container.resolve(IBranchTransferService)  # type: ignore[type-abstract]


def _query(request: HttpRequest) -> QuerySet[BranchTransfer]:
    """Transfers touching a branch covered by this exact org:read grant.

    Branches themselves are tenant-wide directory data, but transfer rows are an
    audit trail about a person.  A role in Branch A must not receive unrelated
    Branch B -> C personnel movements merely because it can list branch names.
    """
    queryset = _service().list()
    if is_permission_unscoped(request, permission=f"{_RESOURCE}:read"):
        return queryset
    # Transfer rows have immutable branch attribution but no immutable
    # department snapshot. Department-only grants cannot be narrowed safely and
    # therefore see no transfer-person history.
    allowed = permission_membership_branch_wide_ids(
        roles=get_user_roles(request),
        permission=f"{_RESOURCE}:read",
    )
    return queryset.filter(Q(from_branch_id__in=allowed) | Q(to_branch_id__in=allowed))


@csrf_exempt
@require_auth
def transfers_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = apply_filters(
            request,
            _query(request),
            filter_fields=_FILTERS,
            ordering_fields=_ORDERING,
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([transfer_to_dict(t) for t in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        permission = f"{_RESOURCE}:write"
        check_perm(request, permission)
        body = read_json(request)
        reject_unknown_fields(body, allowed=_CREATE_FIELDS)
        subject_kind = trimmed_str_field(body, "subject_kind", max_length=16) or "student"
        if subject_kind not in {
            BranchTransfer.SubjectKind.STUDENT,
            BranchTransfer.SubjectKind.TEACHER,
            BranchTransfer.SubjectKind.STAFF,
            BranchTransfer.SubjectKind.COHORT,
        }:
            return error("Invalid transfer type.", code="validation_error", status=400)
        subject_id = int_field(
            body,
            "student" if subject_kind == BranchTransfer.SubjectKind.STUDENT else "subject",
            required=True,
            min_value=1,
        )
        to_branch_id = int_field(body, "to_branch", required=True, min_value=1)
        from_branch_id = int_field(body, "from_branch", min_value=1)
        to_department_id = int_field(body, "to_department", min_value=1)
        reason = trimmed_str_field(body, "reason", max_length=64)
        confirm_impacts = bool_field(body, "confirm_impacts", default=False)
        roles = get_user_roles(request)
        allowed_branch_ids = (
            None
            if is_permission_unscoped(request, permission=permission)
            else permission_membership_branch_wide_ids(roles=roles, permission=permission)
        )
        shared = {
            "to_branch_id": to_branch_id,
            "reason": reason,
            "actor": request.user,
            "actor_principal_kind": getattr(request, "principal_kind", ""),
            "actor_principal_id": getattr(request, "principal_id", None),
            "allowed_branch_ids": allowed_branch_ids,
        }
        if subject_kind == BranchTransfer.SubjectKind.STUDENT:
            transfer = _service().transfer_student(student_id=subject_id, **shared)
        elif subject_kind == BranchTransfer.SubjectKind.TEACHER:
            transfer = _service().transfer_teacher(
                teacher_id=subject_id,
                to_department_id=to_department_id,
                confirm_impacts=confirm_impacts,
                **shared,
            )
        elif subject_kind == BranchTransfer.SubjectKind.STAFF:
            if from_branch_id is None:
                return error(
                    "Choose the staff member's current branch.",
                    code="validation_error",
                    status=400,
                )
            transfer = _service().transfer_staff(
                staff_id=subject_id,
                from_branch_id=from_branch_id,
                to_department_id=to_department_id,
                **shared,
            )
        else:
            transfer = _service().transfer_cohort(
                cohort_id=subject_id,
                to_department_id=to_department_id,
                confirm_impacts=confirm_impacts,
                **shared,
            )
        return created(transfer_to_dict(transfer))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def transfer_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    transfer = _query(request).filter(pk=pk).first()
    if transfer is None:
        raise NotFoundException(code="not_found")
    return success(transfer_to_dict(transfer))
