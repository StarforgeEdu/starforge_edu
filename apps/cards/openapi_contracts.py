"""Explicit observational contracts for wallet read operations."""

from core.openapi_contracts import (
    SESSION_SECURITY,
    OperationContract,
    error_response,
    json_response,
)


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
