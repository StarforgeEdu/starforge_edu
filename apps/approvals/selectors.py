"""Approval read scoping: handlers (approvers/disbursers) see all; everyone else
sees only their own requests (TD-5)."""

from __future__ import annotations

from apps.approvals.models import ApprovalRequest
from core.permissions import has_permission_code


def scoped_requests(*, user, roles):
    qs = ApprovalRequest.objects.select_related(
        "branch", "requested_by", "decided_by", "disbursed_by", "payment_method", "ledger_entry"
    )
    if user.is_superuser:
        return qs
    if has_permission_code(roles, "approvals:approve") or has_permission_code(roles, "approvals:disburse"):
        return qs
    return qs.filter(requested_by=user)
