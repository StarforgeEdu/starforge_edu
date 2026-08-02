"""Explicit state-transition contracts for achievement decisions."""

from core.openapi_contracts import (
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_response,
)


def _decision_contract(*, approve: bool) -> OperationContract:
    action = "approve" if approve else "reject"
    past_tense = "approved" if approve else "rejected"
    return OperationContract(
        method="POST",
        summary=f"{action.title()} a pending achievement",
        description=(
            f"Atomically {action}s a visible pending achievement. This is a state-changing "
            "operation and accepts neither GET nor a request body."
        ),
        permission="achievements:approve",
        security=UNSAFE_SESSION_SECURITY,
        responses={
            "200": json_response(f"Achievement {past_tense}.", "Success"),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("Permission, scope, read-only, or cookie CSRF check failed."),
            "404": error_response("Achievement is absent or outside the caller's visible scope."),
            "422": error_response("Achievement is no longer pending."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id=f"post_achievements_{action}",
    )


APPROVE_CONTRACT = _decision_contract(approve=True)
REJECT_CONTRACT = _decision_contract(approve=False)
