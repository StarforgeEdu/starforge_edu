"""Approval request row scoping.

Requesters see their own requests.  Branch handlers see requests only in the
branch of the *same membership* that grants the relevant approval permission;
an approval grant in one branch must never borrow an unrelated membership in
another branch.  Directors and superusers retain tenant-wide visibility.
"""

from __future__ import annotations

from django.db.models import Q

from apps.approvals.models import ApprovalRequest
from core.permissions import PermissionRoleSet, Role
from core.scoping import permission_membership_scope_q, permission_membership_scopes


def _handler_read_scope_q(roles) -> Q:
    """Branches where the caller can both read and handle approvals.

    Grants are additive at one exact scope boundary, so separate account types in
    the same branch may supply the read and handler halves.  They must never be
    combined across different branches.  An organization-wide half intersects
    with the other half's narrower branches; only two organization-wide halves
    produce tenant-wide visibility (including branchless requests).
    """
    read_scopes = permission_membership_scopes(roles=roles, permission="approvals:read")
    handler_scopes = (
        *permission_membership_scopes(roles=roles, permission="approvals:approve"),
        *permission_membership_scopes(roles=roles, permission="approvals:disburse"),
    )
    read_unscoped = any(scope.is_organization_wide for scope in read_scopes)
    handler_unscoped = any(scope.is_organization_wide for scope in handler_scopes)
    if read_unscoped and handler_unscoped:
        return Q(pk__isnull=False)

    read_branch_ids = {scope.branch_id for scope in read_scopes if not scope.is_organization_wide}
    handler_branch_ids = {scope.branch_id for scope in handler_scopes if not scope.is_organization_wide}
    if read_unscoped:
        branch_ids = handler_branch_ids
    elif handler_unscoped:
        branch_ids = read_branch_ids
    else:
        branch_ids = read_branch_ids & handler_branch_ids
    return Q(branch_id__in=branch_ids) if branch_ids else Q(pk__in=[])


def scoped_requests(
    *,
    user,
    roles,
    permission: str | None = "approvals:read",
    include_requested_by: bool = True,
):
    qs = ApprovalRequest.objects.select_related(
        "branch", "requested_by", "decided_by", "disbursed_by", "payment_method", "ledger_entry"
    )
    effective_roles = roles if roles is not None else ()
    if user.is_superuser:
        return qs

    # Plain sets are retained only for direct legacy domain tests.  Request
    # paths carry PermissionRoleSet, where an owner identity may not lend global
    # scope to a permission granted by a different membership.
    if not isinstance(effective_roles, PermissionRoleSet) and Role.DIRECTOR in effective_roles:
        return qs

    if permission == "approvals:read":
        scope = _handler_read_scope_q(effective_roles)
    elif permission is None:
        scope = Q(pk__in=[])
    else:
        scope = permission_membership_scope_q(
            roles=effective_roles,
            permission=permission,
            branch_field="branch_id",
        )
    if include_requested_by:
        scope |= Q(requested_by=user)
    return qs.filter(scope)
