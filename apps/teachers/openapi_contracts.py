"""Executable contracts for compensation-sensitive teacher operations."""

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)


def _policy_read(method: str) -> OperationContract:
    return OperationContract(
        method=method,
        summary="Read a teacher payout policy",
        description=(
            "Returns the active payout-policy configuration at the exact organization "
            "scope granting compensation:read. This capability is independent of finance "
            "and faculty-directory access."
        ),
        permission="compensation:read",
        security=SESSION_SECURITY,
        responses={
            "200": json_response("Payout policy.", "PayoutPolicyResponse")
            if method == "GET"
            else json_response("Payout policy is visible."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The session lacks compensation read authority."),
            "404": error_response("The teacher or payout policy is not visible."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id=f"{method.lower()}_teachers_payout_policy",
    )


def _policy_write(method: str) -> OperationContract:
    return OperationContract(
        method=method,
        summary="Set a teacher payout policy",
        description=(
            "Creates or replaces the payout method and its exact decimal parameter. JSON "
            "numbers and unknown fields are rejected; money is sent as a decimal string."
        ),
        permission="compensation:write",
        security=UNSAFE_SESSION_SECURITY,
        request_body=json_request("PayoutPolicyRequest"),
        responses={
            "200": json_response("Payout policy stored.", "PayoutPolicyResponse"),
            "400": error_response("The method, decimal parameter, or request shape is invalid."),
            "401": error_response("Session is absent, invalid, expired, or revoked."),
            "403": error_response("The session lacks scoped compensation write authority."),
            "404": error_response("The teacher is outside the caller's compensation scope."),
            "429": error_response("Authenticated request rate limit exceeded."),
        },
        operation_id=f"{method.lower()}_teachers_payout_policy",
    )


PAYOUT_POLICY_CONTRACTS = (
    _policy_read("GET"),
    _policy_read("HEAD"),
    _policy_write("POST"),
    _policy_write("PUT"),
)


PREPARE_SALARY_CONTRACT = OperationContract(
    method="POST",
    summary="Prepare one idempotent salary request",
    description=(
        "Computes the teacher's payout for the inclusive period and creates one maker-checker "
        "approval request. Only completed lessons count toward hourly pay; flat-monthly rules "
        "require one completed calendar month. Idempotency-Key is mandatory; both key reuse and "
        "multiple keys for the same teacher/period resolve to one request. Reused mismatched keys, "
        "closed exact periods, and overlapping active or disbursed periods return 409."
    ),
    permission="compensation:run",
    security=UNSAFE_SESSION_SECURITY,
    parameters=(
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "minLength": 16, "maxLength": 128},
            "description": "Visible ASCII retry key; only its tenant/actor-scoped hash is stored.",
        },
    ),
    request_body=json_request("SalaryPrepareRequest"),
    responses={
        "201": json_response("Salary approval request prepared.", "SalaryPrepareResponse"),
        "400": error_response("The period, key, or request shape is invalid."),
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "403": error_response("The session lacks scoped compensation run authority."),
        "404": error_response("The teacher is outside the caller's compensation scope."),
        "409": error_response(
            "The idempotency key was reused, or the requested salary period is closed or overlaps."
        ),
        "422": error_response("The payout policy is missing, invalid, or computes zero."),
        "429": error_response("Authenticated request rate limit exceeded."),
    },
    operation_id="post_teachers_prepare_salary",
)
