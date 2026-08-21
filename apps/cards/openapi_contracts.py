"""Explicit observational contracts for wallet read operations."""

from core.openapi_contracts import (
    SESSION_SECURITY,
    UNSAFE_SESSION_SECURITY,
    OperationContract,
    error_response,
    json_request,
    json_response,
)

IDEMPOTENCY_HEADER = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": True,
    "schema": {"type": "string", "minLength": 16, "maxLength": 128},
    "description": ("Visible ASCII retry key. Only its tenant/role-principal-scoped SHA-256 hash is stored."),
}
STUDENT_ID_PARAMETER = {
    "name": "student_id",
    "in": "path",
    "required": True,
    "schema": {"type": "integer", "minimum": 1},
    "description": "Public StudentProfile identifier resolved inside the caller's current branch scope.",
}


def _wallet_read_contracts(*, self_service: bool) -> tuple[OperationContract, OperationContract]:
    subject = "current student's" if self_service else "scoped student's"
    operation_suffix = "me" if self_service else "student"
    permission = None if self_service else "wallet:read"
    not_found = (
        "The current principal has no student profile."
        if self_service
        else "Student is absent or outside the caller's branch scope."
    )
    common_errors = {
        "401": error_response("Session is absent, invalid, expired, or revoked."),
        "404": error_response(not_found),
        "429": error_response("Authenticated request rate limit exceeded."),
    }
    if not self_service:
        common_errors["403"] = error_response("The principal lacks wallet read authority.")
    description = (
        f"Reads the {subject} existing wallet and recent transactions. A missing wallet is "
        "represented as `wallet: null`; GET and HEAD never provision a wallet or write any row."
    )
    return (
        OperationContract(
            method="GET",
            summary=f"Read the {subject} wallet",
            description=description,
            permission=permission,
            security=SESSION_SECURITY,
            responses={
                "200": json_response(
                    "Existing wallet state, or an explicit null wallet.",
                    "WalletPayloadResponse",
                ),
                **common_errors,
            },
            operation_id=f"get_cards_wallet_{operation_suffix}",
        ),
        OperationContract(
            method="HEAD",
            summary=f"Check the {subject} wallet",
            description=f"{description} The response body is omitted.",
            permission=permission,
            security=SESSION_SECURITY,
            responses={
                "200": json_response("Wallet lookup completed without mutation."),
                **common_errors,
            },
            operation_id=f"head_cards_wallet_{operation_suffix}",
        ),
    )


WALLET_ME_CONTRACTS = _wallet_read_contracts(self_service=True)
STUDENT_WALLET_CONTRACTS = _wallet_read_contracts(self_service=False)


def _wallet_write_contract(*, action: str) -> tuple[OperationContract]:
    operation = "top up" if action == "topup" else action
    return (
        OperationContract(
            method="POST",
            summary=f"{operation.title()} a scoped student's wallet",
            description=(
                "Requires wallet:write in the student's current branch. The mandatory "
                "Idempotency-Key is scoped to the exact authenticated role principal; an exact "
                "retry returns the original transaction, while reuse for another action, student, "
                "amount, or note returns 409. Authorization and current branch scope are checked "
                "again on every replay."
            ),
            permission="wallet:write",
            security=UNSAFE_SESSION_SECURITY,
            parameters=(STUDENT_ID_PARAMETER, IDEMPOTENCY_HEADER),
            request_body=json_request("WalletAmountRequest"),
            responses={
                "201": json_response("The stored wallet transaction.", "WalletTransactionResponse"),
                "400": error_response("The JSON body or idempotency key is invalid."),
                "401": error_response("The session is absent, invalid, expired, or revoked."),
                "402": error_response("The tenant subscription does not include this capability."),
                "403": error_response("The principal lacks wallet write authority in this branch."),
                "404": error_response("The student is absent or outside the current visible scope."),
                "409": error_response("The idempotency key belongs to another wallet operation."),
                "422": error_response("The requested balance transition is not valid."),
                "429": error_response("The authenticated request rate limit was exceeded."),
            },
            operation_id=f"post_cards_wallet_{action}",
        ),
    )


WALLET_TOPUP_CONTRACTS = _wallet_write_contract(action="topup")
WALLET_SPEND_CONTRACTS = _wallet_write_contract(action="spend")
WALLET_REFUND_CONTRACTS = _wallet_write_contract(action="refund")
