"""Explicit OpenAPI contracts for security- and product-critical views.

The project still has many legacy function views whose methods are discovered by
source inspection.  That is useful as a compatibility inventory, but it is not a
safe contract for login, authorization bootstrap, or state transitions: a helper
call hidden behind a wrapper can otherwise be mis-published as ``GET``.

``openapi_contract`` attaches immutable, per-operation metadata to the actual
Django callback.  :mod:`core.openapi` validates the expected path and the view's
independently detected runtime methods before publishing a critical contract.  A
drifted critical operation therefore fails schema generation instead of silently
falling back to guessed metadata.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

_View = TypeVar("_View", bound=Callable[..., Any])


class OpenAPIContractError(RuntimeError):
    """The executable route and its explicit public contract disagree."""


@dataclass(frozen=True)
class OperationContract:
    """One fully declared HTTP operation.

    ``security`` uses OpenAPI's operation-level representation.  An empty tuple
    explicitly means public; ``None`` is deliberately forbidden on critical
    contracts so authentication can never be inherited or guessed accidentally.
    """

    method: str
    summary: str
    responses: Mapping[str, Mapping[str, Any]]
    security: tuple[Mapping[str, Sequence[str]], ...] | None
    description: str = ""
    permission: str | None = None
    request_body: Mapping[str, Any] | None = None
    parameters: tuple[Mapping[str, Any], ...] = ()
    operation_id: str | None = None

    def __post_init__(self) -> None:
        method = self.method.upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"Unsupported OpenAPI method: {self.method!r}")
        object.__setattr__(self, "method", method)
        if not self.responses:
            raise ValueError(f"{method} contract must declare at least one response")


@dataclass(frozen=True)
class ViewContract:
    expected_path: str
    operations: tuple[OperationContract, ...]
    critical: bool = True
    exact_methods: bool = True

    def __post_init__(self) -> None:
        if not self.expected_path.startswith("/"):
            raise ValueError("An OpenAPI contract path must start with '/'.")
        methods = [operation.method for operation in self.operations]
        if not methods or len(methods) != len(set(methods)):
            raise ValueError("A view contract needs unique operation methods.")
        if self.critical and any(operation.security is None for operation in self.operations):
            raise ValueError("Critical OpenAPI operations must declare security explicitly.")


def openapi_contract(
    *,
    path: str,
    operations: tuple[OperationContract, ...],
    critical: bool = True,
    exact_methods: bool = True,
) -> Callable[[_View], _View]:
    """Attach an explicit contract to a Django callback without wrapping it."""

    contract = ViewContract(
        expected_path=path,
        operations=operations,
        critical=critical,
        exact_methods=exact_methods,
    )

    def decorator(view: _View) -> _View:
        existing = getattr(view, "__openapi_contract__", None)
        if existing is not None and existing != contract:
            raise OpenAPIContractError(f"{view.__module__}.{view.__name__} has two OpenAPI contracts")
        view.__openapi_contract__ = contract  # type: ignore[attr-defined]
        return view

    return decorator


def get_openapi_contract(callback: Callable[..., Any]) -> ViewContract | None:
    """Return metadata copied through ``functools.wraps`` decorator layers."""

    contract = getattr(callback, "__openapi_contract__", None)
    if contract is None:
        return None
    if not isinstance(contract, ViewContract):
        raise OpenAPIContractError("Invalid __openapi_contract__ metadata on URL callback")
    return contract


def schema_ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def json_request(schema_name: str, *, required: bool = True) -> dict[str, Any]:
    return {
        "required": required,
        "content": {"application/json": {"schema": schema_ref(schema_name)}},
    }


def json_response(description: str, schema_name: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"description": description}
    if schema_name is not None:
        response["content"] = {"application/json": {"schema": schema_ref(schema_name)}}
    return response


def error_response(description: str, schema_name: str = "Error") -> dict[str, Any]:
    return json_response(description, schema_name)


PUBLIC_SECURITY: tuple[Mapping[str, Sequence[str]], ...] = ()
SESSION_SECURITY: tuple[Mapping[str, Sequence[str]], ...] = (
    {"sessionAuth": ()},
    {"cookieSession": ()},
)
UNSAFE_SESSION_SECURITY: tuple[Mapping[str, Sequence[str]], ...] = (
    {"sessionAuth": ()},
    {"cookieSession": (), "csrfHeader": ()},
)
BRANCH_AGENT_SECURITY: tuple[Mapping[str, Sequence[str]], ...] = ({"branchAgentAuth": ()},)
OPTIONAL_LOGOUT_SECURITY: tuple[Mapping[str, Sequence[str]], ...] = (
    {},
    {"sessionAuth": ()},
    {"cookieSession": (), "csrfHeader": ()},
)
