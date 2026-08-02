"""Custom OpenAPI 3.0 schema builder for the off-DRF layered API.

`drf-spectacular` only introspects DRF views (APIViews / ViewSets). 37 of the 38 apps are plain
Django function views, so the auto-generated schema covered only the lone remaining DRF app
(`reports`) — Swagger showed ~5 of ~320 endpoints and no client could generate a typed SDK.

This builder walks the active URLconf and, for every ``/api/v1/`` endpoint, publishes:

* the OpenAPI **path** (Django ``<int:pk>`` → ``{pk}``) + typed path parameters,
* explicit, fail-closed operation contracts for security- and product-critical routes;
* a compatibility inventory for legacy routes, whose methods and permissions are still
  introspected from their ``request.method`` / ``check_perm`` branches;
* the required **permission** (from ``check_perm(request, "resource:action")``),
* **auth** (Bearer or HttpOnly cookie + CSRF for explicit operations; legacy endpoints use a
  narrow exact public allowlist and otherwise require Bearer authentication),
* the project's **standard response envelope** (success / paginated / flat error).

The result is a complete, valid OpenAPI 3.0.3 document that Swagger UI / Redoc render and that
`openapi-generator` / `swagger-codegen` can turn into a TypeScript or Dart client. It is built
once per process per URLconf (source never changes at runtime) and cached.
"""

from __future__ import annotations

import inspect
import re
from copy import deepcopy
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.urls import URLPattern, URLResolver, get_resolver

from core.openapi_contracts import (
    OpenAPIContractError,
    OperationContract,
    ViewContract,
    get_openapi_contract,
)

_API_PREFIX = "api/v1/"

# Legacy public operations. Exact paths prevent a future route whose name merely
# contains "auth/login" from accidentally losing authentication in the schema.
_PUBLIC_PATHS = frozenset(
    {
        "/api/v1/auth/login/",
        "/api/v1/auth/password/reset/request/",
        "/api/v1/auth/password/reset/confirm/",
        "/api/v1/platform/resolve/",
    }
)
_PUBLIC_PATH_PREFIXES = ("/api/v1/webhooks/",)
_PUBLIC_SCHEMA_PATH_PREFIXES = ("/api/v1/platform/", "/api/v1/webhooks/")
_SUBSCRIPTION_ALLOWLIST_PREFIXES = ("/api/v1/auth/",)

_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_METHOD_RE = re.compile(r'"(GET|POST|PUT|PATCH|DELETE)"')
_RUNTIME_HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
_RUNTIME_METHOD_RE = re.compile(r'["\'](GET|HEAD|POST|PUT|PATCH|DELETE)["\']')
_PERM_RE = re.compile(r'check_perm\(\s*request\s*,\s*f?"([a-z_]+:[a-z_*]+)"')
_RESOURCE_RE = re.compile(r'_RESOURCE\s*=\s*"([a-z_]+)"')
# `<int:pk>` / `<slug:center_slug>` / `<pk>` -> (converter, name)
_PARAM_RE = re.compile(r"<(?:(?P<conv>[a-zA-Z_]+):)?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)>")
_CONVERTER_TYPE = {
    "int": ("integer", None),
    "str": ("string", None),
    "slug": ("string", None),
    "uuid": ("string", "uuid"),
    "path": ("string", None),
}


def _route_of(pattern: Any) -> str | None:
    """The route template of a URLPattern/URLResolver as an OpenAPI path fragment.

    Django ``path()`` gives a RoutePattern (``._route`` like ``students/`` / ``<int:pk>/``).
    A DRF router (the lone DRF ``reports`` app) or ``re_path`` gives a RegexPattern (``._regex``,
    no ``._route``) — translate its named groups to ``{name}`` and SKIP the ``.json``/``.api``
    format-suffix routes DRF adds. ``None`` => the caller skips this entry.
    """
    p = getattr(pattern, "pattern", None)
    if p is None:
        return ""
    route = getattr(p, "_route", None)
    if route is not None:
        return route  # django path()
    rx = getattr(p, "_regex", "") or ""
    if not rx or "?P<format>" in rx:  # a DRF format-suffix (.json / .api) route — skip
        return None
    frag = re.sub(r"\(\?P<(\w+)>[^)]*\)", r"{\1}", rx.lstrip("^").rstrip("$"))
    # Bail on anything still carrying regex metacharacters we can't render as a clean path.
    return None if any(c in frag for c in "()[]\\+*?|") else frag


def _walk(patterns: list, prefix: str) -> list[tuple[str, Any, str]]:
    """Flatten a urlpatterns tree into ``(full_route, callback, name)`` leaves."""
    out: list[tuple[str, Any, str]] = []
    for entry in patterns:
        frag = _route_of(entry)
        if frag is None:  # a deliberately-skipped route (DRF format-suffix / untranslatable)
            continue
        route = prefix + frag
        if isinstance(entry, URLResolver):
            out.extend(_walk(entry.url_patterns, route))
        elif isinstance(entry, URLPattern):
            out.append((route, entry.callback, entry.name or ""))
    return out


def _openapi_path(route: str) -> tuple[str, list[dict]]:
    """``api/v1/students/<int:pk>/`` -> (``/api/v1/students/{pk}/``, [pk param])."""
    params: list[dict] = []
    for m in _PARAM_RE.finditer(route):
        name = m.group("name")
        typ, fmt = _CONVERTER_TYPE.get(m.group("conv") or "str", ("string", None))
        schema: dict[str, Any] = {"type": typ}
        if fmt:
            schema["format"] = fmt
        params.append({"name": name, "in": "path", "required": True, "schema": schema})
    path = "/" + _PARAM_RE.sub(lambda m: "{" + m.group("name") + "}", route)
    return path, params


def _view_source(callback: Any) -> str:
    """Source of the underlying view (unwrapping @require_auth / @csrf_exempt @wraps layers)."""
    try:
        return inspect.getsource(inspect.unwrap(callback))
    except (OSError, TypeError):
        return ""


def _methods_and_meta(callback: Any) -> tuple[list[str], str | None, str]:
    """(http_methods, required_permission, module_resource) introspected from the view."""
    # DRF viewset route (as_view({"get":"list","post":"create",...})): the real method set is
    # the actions map, NOT the request.method branches (unwrap resolves to APIView.dispatch).
    actions = getattr(callback, "actions", None)
    if actions:
        # The DRF router maps every mixin verb (e.g. PUT+PATCH for UpdateModelMixin), but the
        # viewset may narrow that via http_method_names (ReportScheduleViewSet drops PUT).
        allowed = getattr(getattr(callback, "cls", None), "http_method_names", None)
        allow = {m.upper() for m in allowed} if allowed else set(_HTTP_METHODS)
        drf_methods = sorted(
            {m.upper() for m in actions if m.upper() in _HTTP_METHODS and m.upper() in allow},
            key=_HTTP_METHODS.index,
        )
        return (drf_methods or ["GET"]), None, ""
    src = _view_source(callback)
    methods: set[str] = set()
    # (1) Django method-restricting decorators (the decorator header sits above `def`).
    header = src.split("\ndef ", 1)[0]
    if "require_POST" in header:
        methods.add("POST")
    if "require_GET" in header:
        methods.add("GET")
    http_methods = re.search(r"require_http_methods\(\s*\[([^\]]*)\]", header)
    if http_methods:
        methods.update(_METHOD_RE.findall(http_methods.group(1)))
    # (2) Views that branch on request.method internally (the common layered pattern).
    for line in src.splitlines():
        if "request.method" in line:
            methods.update(_METHOD_RE.findall(line))
    if not methods:
        methods = {"GET"}  # a view with no method branch is a single-method (GET) feed
    perm_match = _PERM_RE.search(src)
    perm = perm_match.group(1) if perm_match else None
    res_match = _RESOURCE_RE.search(src)
    resource = res_match.group(1) if res_match else ""
    # An f-string perm like f"{_RESOURCE}:read" resolves to the module _RESOURCE.
    if perm is None and resource:
        perm = f"{resource}:*"
    return sorted(methods, key=_HTTP_METHODS.index), perm, resource


def _runtime_methods(callback: Any) -> tuple[str, ...]:
    """Independently detect the methods the executable callback accepts.

    Unlike the legacy inventory inference, this helper has no GET fallback.  A
    critical explicit contract must be provable from a method decorator, a DRF
    action map, or a real ``request.method`` branch in the registered view.
    """

    actions = getattr(callback, "actions", None)
    if actions:
        allowed = getattr(getattr(callback, "cls", None), "http_method_names", None)
        allow = {method.upper() for method in allowed} if allowed else set(_RUNTIME_HTTP_METHODS)
        return tuple(
            sorted(
                {
                    method.upper()
                    for method in actions
                    if method.upper() in _RUNTIME_HTTP_METHODS and method.upper() in allow
                },
                key=_RUNTIME_HTTP_METHODS.index,
            )
        )

    source = _view_source(callback)
    header = source.split("\ndef ", 1)[0]
    methods: set[str] = set()
    if "require_POST" in header:
        methods.add("POST")
    if "require_GET" in header:
        methods.add("GET")
    if "require_safe" in header:
        methods.update(("GET", "HEAD"))
    restricted = re.search(r"require_http_methods\(\s*\[([^\]]*)\]", header)
    if restricted:
        methods.update(_RUNTIME_METHOD_RE.findall(restricted.group(1)))
    for line in source.splitlines():
        if "request.method" in line:
            methods.update(_RUNTIME_METHOD_RE.findall(line))
    return tuple(sorted(methods, key=_RUNTIME_HTTP_METHODS.index))


def _tag_for(path: str) -> str:
    """Group operations by their mount (``/api/v1/<mount>/...`` -> ``mount``)."""
    parts = [p for p in path.split("/") if p and p not in ("api", "v1")]
    return parts[0] if parts else "root"


def _summary(name: str, method: str, path: str) -> str:
    if name:
        return f"{method} {name.replace('-', ' ')}"
    return f"{method} {path}"


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES)


def _operation(*, method: str, name: str, path: str, perm: str | None, public: bool) -> dict:
    # URL names are only unique inside an app namespace (for example both
    # schedule and rulebook expose ``rule-list``).  Include the mount tag so the
    # document satisfies OpenAPI's global operationId uniqueness requirement.
    operation_name = name or path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    op: dict[str, Any] = {
        "operationId": f"{method.lower()}_{_tag_for(path)}_{operation_name}".replace("-", "_"),
        "summary": _summary(name, method, path),
        "tags": [_tag_for(path)],
        "responses": _runtime_responses(path=path, responses=_responses(method)),
    }
    desc = []
    if perm:
        desc.append(f"Requires permission `{perm}`.")
    if public:
        desc.append("Public endpoint — no authentication required.")
    else:
        op["security"] = [{"sessionAuth": []}]
    if desc:
        op["description"] = " ".join(desc)
    if method in ("POST", "PUT", "PATCH"):
        op["requestBody"] = {
            "required": method != "PATCH",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    if method == "GET" and not path.rstrip("/").endswith("}"):
        # A collection GET supports the standard listing query params.
        op["parameters"] = [
            {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
            {"name": "page_size", "in": "query", "schema": {"type": "integer", "minimum": 1}},
            {
                "name": "ordering",
                "in": "query",
                "schema": {"type": "string"},
                "description": "Field to sort by; prefix with `-` for descending.",
            },
            {"name": "search", "in": "query", "schema": {"type": "string"}},
        ]
    return op


def _contract_operation(*, contract: OperationContract, name: str, path: str) -> dict[str, Any]:
    """Render one explicit operation without verb- or path-based guesses."""

    operation_name = name or path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    op: dict[str, Any] = {
        "operationId": contract.operation_id
        or f"{contract.method.lower()}_{_tag_for(path)}_{operation_name}".replace("-", "_"),
        "summary": contract.summary,
        "tags": [_tag_for(path)],
        "responses": _runtime_responses(
            path=path,
            responses=deepcopy(dict(contract.responses)),
        ),
    }
    description: list[str] = []
    if contract.description:
        description.append(contract.description)
    if contract.permission:
        description.append(f"Requires permission `{contract.permission}`.")
    if contract.security == ():
        description.append("Public endpoint — no authentication required.")
    elif contract.security is not None:
        op["security"] = [
            {scheme: list(scopes) for scheme, scopes in requirement.items()}
            for requirement in contract.security
        ]
    if description:
        op["description"] = " ".join(description)
    if contract.request_body is not None:
        op["requestBody"] = deepcopy(dict(contract.request_body))
    if contract.parameters:
        op["parameters"] = deepcopy(list(contract.parameters))
    return op


def _runtime_responses(*, path: str, responses: dict[str, Any]) -> dict[str, Any]:
    """Add responses produced before any view callback can execute.

    Subscription, tenant-lifecycle, and app-availability middleware sit ahead of
    both DRF and layered views. Their 402/503 envelopes are therefore part of
    every tenant operation's executable contract, even when the view itself
    cannot raise those statuses.
    """

    if path.startswith(_PUBLIC_SCHEMA_PATH_PREFIXES):
        return responses
    if not path.startswith(_SUBSCRIPTION_ALLOWLIST_PREFIXES):
        responses.setdefault(
            "402",
            {
                "description": "The tenant subscription must be restored before this operation is available.",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/SubscriptionRequiredError"}}
                },
            },
        )
    responses.setdefault(
        "503",
        {
            "description": "The tenant or requested capability is temporarily unavailable.",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/TemporarilyUnavailableError"}}
            },
        },
    )
    return responses


def _validate_view_contract(
    *,
    contract: ViewContract,
    callback: Any,
    path: str,
    name: str,
) -> None:
    """Fail closed when critical metadata drifts from the URL/runtime view."""

    label = name or getattr(callback, "__name__", repr(callback))
    if path != contract.expected_path:
        raise OpenAPIContractError(
            f"OpenAPI contract for {label} expects {contract.expected_path}, runtime URL is {path}"
        )
    runtime_methods = _runtime_methods(callback)
    contracted_methods = tuple(operation.method for operation in contract.operations)
    if contract.critical and not runtime_methods:
        raise OpenAPIContractError(f"Cannot prove runtime methods for critical operation {path}")
    if contract.exact_methods and set(runtime_methods) != set(contracted_methods):
        raise OpenAPIContractError(
            f"OpenAPI methods for {path} are {sorted(contracted_methods)}, "
            f"runtime accepts {sorted(runtime_methods)}"
        )

    source = _view_source(callback)
    header = source.split("\ndef ", 1)[0]
    runtime_requires_session_auth = "require_auth" in header
    runtime_requires_branch_agent = "require_branch_agent" in header
    if runtime_requires_session_auth and runtime_requires_branch_agent:
        raise OpenAPIContractError(f"{path} declares two incompatible runtime authentication guards")
    for operation in contract.operations:
        security = operation.security
        if security is None:
            if contract.critical:
                raise OpenAPIContractError(f"{operation.method} {path} has no explicit security contract")
            continue
        anonymous_allowed = security == () or any(not requirement for requirement in security)
        auth_alternative_exists = any(requirement for requirement in security)
        declared_schemes = {scheme for requirement in security for scheme in requirement}
        if runtime_requires_session_auth and anonymous_allowed:
            raise OpenAPIContractError(
                f"{operation.method} {path} requires authentication at runtime but its contract allows anonymous access"
            )
        if runtime_requires_session_auth and (
            "branchAgentAuth" in declared_schemes
            or not declared_schemes.intersection({"sessionAuth", "cookieSession"})
        ):
            raise OpenAPIContractError(
                f"{operation.method} {path} uses session authentication at runtime but declares a different scheme"
            )
        if runtime_requires_branch_agent and anonymous_allowed:
            raise OpenAPIContractError(
                f"{operation.method} {path} requires branch-agent authentication at runtime "
                "but its contract allows anonymous access"
            )
        if runtime_requires_branch_agent and declared_schemes != {"branchAgentAuth"}:
            raise OpenAPIContractError(
                f"{operation.method} {path} uses branch-agent authentication at runtime "
                "but does not exclusively declare branchAgentAuth"
            )
        runtime_has_auth_guard = runtime_requires_session_auth or runtime_requires_branch_agent
        if not runtime_has_auth_guard and auth_alternative_exists and not anonymous_allowed:
            raise OpenAPIContractError(
                f"{operation.method} {path} declares required authentication but runtime has no recognized auth guard"
            )

    body_contract_exists = any(operation.request_body is not None for operation in contract.operations)
    runtime_reads_json = "read_json(request)" in source
    if body_contract_exists and not runtime_reads_json:
        raise OpenAPIContractError(f"{path} declares a JSON body but runtime does not parse one")
    if runtime_reads_json and not body_contract_exists:
        raise OpenAPIContractError(f"{path} parses JSON but its explicit operations declare no request body")


def _responses(method: str) -> dict:
    ok_code = "201" if method == "POST" else ("204" if method == "DELETE" else "200")
    ok: dict[str, Any] = {"description": "Success"}
    if ok_code != "204":
        ok["content"] = {"application/json": {"schema": {"$ref": "#/components/schemas/Success"}}}
    out = {
        ok_code: ok,
        "400": _err("Validation / bad request"),
        "401": _err("Not authenticated"),
        "403": _err("Forbidden"),
        "404": _err("Not found"),
        "429": _err("Rate limited"),
    }
    return out


def _err(desc: str) -> dict:
    return {
        "description": desc,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
    }


def _components() -> dict:
    # Product-critical workflow schemas live beside their executable contracts.
    # Merge them explicitly here so a generated client receives the same closed
    # DTOs and response envelopes validated against the registered callbacks.
    from apps.ai.openapi_contracts import OPENAPI_SCHEMAS as AI_SCHEMAS
    from apps.audit.openapi_contracts import OPENAPI_SCHEMAS as AUDIT_SCHEMAS
    from apps.finance.openapi_contracts import OPENAPI_SCHEMAS as FINANCE_SCHEMAS
    from apps.forms.openapi_contracts import OPENAPI_SCHEMAS as FORM_SCHEMAS
    from apps.meetings.openapi_contracts import OPENAPI_SCHEMAS as MEETING_SCHEMAS
    from apps.payroll.openapi_contracts import PAYROLL_SCHEMAS
    from apps.students.openapi_contracts import OPENAPI_SCHEMAS as STUDENT_SCHEMAS
    from apps.tasks.openapi_contracts import OPENAPI_SCHEMAS as TASK_SCHEMAS
    from apps.users.openapi_contracts import OPENAPI_SCHEMAS as USER_SCHEMAS

    workflow_schemas: dict[str, dict[str, Any]] = {}
    for domain_schemas in (
        AI_SCHEMAS,
        AUDIT_SCHEMAS,
        FORM_SCHEMAS,
        FINANCE_SCHEMAS,
        MEETING_SCHEMAS,
        PAYROLL_SCHEMAS,
        STUDENT_SCHEMAS,
        TASK_SCHEMAS,
        USER_SCHEMAS,
    ):
        duplicates = set(workflow_schemas).intersection(domain_schemas)
        if duplicates:
            raise OpenAPIContractError(f"Duplicate workflow OpenAPI schemas: {', '.join(sorted(duplicates))}")
        workflow_schemas.update(domain_schemas)

    components: dict[str, Any] = {
        "securitySchemes": {
            "sessionAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Opaque session key from `POST /api/v1/auth/role-login/`, sent as "
                    "`Authorization: Bearer <key>`. The tenant is resolved from the request Host."
                ),
            },
            "cookieSession": {
                "type": "apiKey",
                "in": "cookie",
                "name": getattr(settings, "API_SESSION_COOKIE_NAME", "__Host-starforge_session"),
                "description": (
                    "Secure HttpOnly browser session cookie. Obtain a CSRF token from "
                    "`GET /api/v1/auth/session/` before cookie login or an unsafe request."
                ),
            },
            "csrfHeader": {
                "type": "apiKey",
                "in": "header",
                "name": "X-CSRFToken",
                "description": "Required with cookie authentication on unsafe HTTP methods.",
            },
            "branchAgentAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "Authorization",
                "description": (
                    "Branch print-daemon credential, sent exactly as `Authorization: Agent "
                    "<64-lowercase-hex-token>`. It is tenant- and branch-bound, is returned only "
                    "once at registration, and is not a staff/user session."
                ),
            },
        },
        "schemas": {
            "Success": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "example": True},
                    "data": {"description": "Endpoint-specific payload (object or array)."},
                    "pagination": {"$ref": "#/components/schemas/Pagination"},
                    "warnings": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/RuntimeWarning"},
                        "description": "Present only when a soft dependency is degraded (fault isolation).",
                    },
                },
                "required": ["success"],
            },
            "Pagination": {
                "type": "object",
                "properties": {
                    "total": {"type": "integer"},
                    "page": {"type": "integer"},
                    "page_size": {"type": "integer"},
                    "pages": {"type": "integer"},
                    "has_next": {"type": "boolean"},
                    "has_prev": {"type": "boolean"},
                },
            },
            "Error": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "example": False},
                    "code": {
                        "type": "string",
                        "description": "Stable, machine-branchable error code.",
                        "example": "validation_error",
                    },
                    "message": {"type": "string", "description": "Human-readable (localized) detail."},
                    "errors": {"type": "object", "description": "Optional per-field validation errors."},
                    "request_id": {
                        "type": "string",
                        "description": "Correlation identifier when one was assigned to the request.",
                    },
                },
                "required": ["success", "code", "message"],
            },
            "SubscriptionRequiredError": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "success": {"type": "boolean", "enum": [False]},
                    "code": {"type": "string", "enum": ["subscription_required"]},
                    "message": {"type": "string"},
                },
                "required": ["success", "code", "message"],
            },
            "TemporarilyUnavailableError": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "success": {"type": "boolean", "enum": [False]},
                    "code": {
                        "type": "string",
                        "enum": ["center_inactive", "service_unavailable", "temporarily_unavailable"],
                    },
                    "message": {"type": "string"},
                },
                "required": ["success", "code", "message"],
            },
            "RuntimeWarning": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string", "enum": ["information_delayed"]},
                    "message": {"type": "string"},
                    "affected_sections": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string"},
                    },
                },
                "required": ["code", "message", "affected_sections"],
            },
            "FieldErrors": {
                "type": "object",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "RoleLoginRequest": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "minLength": 1, "maxLength": 150},
                    "password": {
                        "type": "string",
                        "format": "password",
                        "minLength": 1,
                        "maxLength": 1024,
                    },
                    "device_id": {
                        "type": "string",
                        "maxLength": 128,
                        "description": "Stable client-generated device identifier; optional.",
                    },
                    "platform": {
                        "type": "string",
                        "maxLength": 32,
                        "description": "Optional client platform label; persisted device values are bounded to 16 characters.",
                    },
                },
                "required": ["username", "password"],
            },
            "RoleLoginData": {
                "type": "object",
                "properties": {
                    "access": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Opaque bearer credential. Present for bearer transport; omitted when "
                            "`X-Session-Transport: cookie` stores it in an HttpOnly cookie."
                        ),
                    },
                    "role": {
                        "type": "string",
                        "enum": ["student", "teacher", "parent", "staff"],
                        "description": "Technical role-native principal kind, not a management product role.",
                    },
                    "must_change_password": {"type": "boolean"},
                },
                "required": ["role", "must_change_password"],
            },
            "RoleLoginResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {"$ref": "#/components/schemas/RoleLoginData"},
                },
                "required": ["success", "data"],
            },
            "SessionBootstrapResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {
                        "type": "object",
                        "properties": {"csrf_token": {"type": "string", "minLength": 1}},
                        "required": ["csrf_token"],
                    },
                },
                "required": ["success", "data"],
            },
            "LogoutResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "message": {"type": "string"},
                },
                "required": ["success", "message"],
            },
            "PasswordChangeRequest": {
                "type": "object",
                "properties": {
                    "old_password": {
                        "type": "string",
                        "format": "password",
                        "maxLength": 1024,
                    },
                    "new_password": {
                        "type": "string",
                        "format": "password",
                        "minLength": 10,
                        "maxLength": 128,
                        "description": "Raw, untrimmed value evaluated by all configured password validators.",
                    },
                },
                "required": ["old_password", "new_password"],
            },
            "PasswordChangeData": {
                "type": "object",
                "properties": {
                    "access": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Replacement bearer credential; omitted for cookie transport.",
                    }
                },
            },
            "PasswordChangeResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {"$ref": "#/components/schemas/PasswordChangeData"},
                },
                "required": ["success", "data"],
            },
            "WrongPasswordError": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [False]},
                    "code": {"type": "string", "enum": ["wrong_password"]},
                    "message": {"type": "string"},
                    "errors": {
                        "type": "object",
                        "properties": {
                            "old_password": {
                                "type": "array",
                                "items": {"type": "string"},
                            }
                        },
                        "required": ["old_password"],
                    },
                },
                "required": ["success", "code", "message", "errors"],
            },
            "WeakPasswordError": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [False]},
                    "code": {"type": "string", "enum": ["weak_password"]},
                    "message": {"type": "string"},
                    "errors": {
                        "type": "object",
                        "properties": {
                            "new_password": {
                                "type": "array",
                                "items": {"type": "string"},
                            }
                        },
                        "required": ["new_password"],
                    },
                },
                "required": ["success", "code", "message", "errors"],
            },
            "ScopeLabel": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string", "nullable": True},
                },
                "required": ["id", "name"],
            },
            "RoleMembership": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "account_type": {"type": "integer", "nullable": True},
                    "account_type_slug": {"type": "string"},
                    "account_type_name": {"type": "string"},
                    "account_kind": {"type": "string"},
                    "legacy_role": {"type": "string"},
                    "branch": {"type": "integer", "nullable": True},
                    "branch_name": {"type": "string", "nullable": True},
                    "department": {"type": "integer", "nullable": True},
                    "department_name": {"type": "string", "nullable": True},
                    "granted_at": {"type": "string", "format": "date-time"},
                },
                "required": ["id", "account_type", "branch", "branch_name", "department", "department_name"],
            },
            "PermissionScope": {
                "type": "object",
                "properties": {
                    "branch": {
                        "nullable": True,
                        "allOf": [{"$ref": "#/components/schemas/ScopeLabel"}],
                    },
                    "department": {
                        "nullable": True,
                        "allOf": [{"$ref": "#/components/schemas/ScopeLabel"}],
                    },
                    "effective_permissions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["branch", "department", "effective_permissions"],
            },
            "UserBootstrapData": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Current role-native identity plus the effective authorization context. "
                    "Clients admit management roles from active memberships, never from "
                    "`principal_kind` alone."
                ),
                "properties": {
                    "id": {"type": "integer"},
                    "principal_kind": {
                        "type": "string",
                        "enum": ["student", "teacher", "parent", "staff"],
                    },
                    "username": {"type": "string"},
                    "phone": {"type": "string", "nullable": True},
                    "email": {"type": "string", "format": "email", "nullable": True},
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "middle_name": {"type": "string"},
                    "full_name": {"type": "string"},
                    "birthdate": {"type": "string", "format": "date", "nullable": True},
                    "gender": {"type": "string", "enum": ["", "m", "f"]},
                    "preferred_language": {"type": "string", "enum": ["uz", "ru", "en"]},
                    "is_active": {"type": "boolean"},
                    "must_change_password": {"type": "boolean"},
                    "last_login_at": {"type": "string", "format": "date-time", "nullable": True},
                    "student_id": {"type": "string"},
                    "status": {"type": "string"},
                    "branch": {"type": "integer", "nullable": True},
                    "current_cohort": {"type": "integer", "nullable": True},
                    "department": {"type": "integer", "nullable": True},
                    "role_memberships": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/RoleMembership"},
                    },
                    "effective_permissions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "uniqueItems": True,
                    },
                    "scopes": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/PermissionScope"},
                    },
                    "organization_locale": {"type": "string"},
                    "organization_timezone": {
                        "type": "string",
                        "example": "Asia/Tashkent",
                        "description": "Authoritative organization-wide IANA timezone.",
                    },
                    "primary_currency": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 3,
                        "example": "UZS",
                    },
                    "read_only_session": {"type": "boolean"},
                    "session_id": {"type": "integer"},
                    "session_created_at": {"type": "string", "format": "date-time"},
                    "session_last_activity_at": {"type": "string", "format": "date-time"},
                    "session_expires_at": {"type": "string", "format": "date-time"},
                    "session_idle_expires_at": {"type": "string", "format": "date-time"},
                    "server_time": {"type": "string", "format": "date-time"},
                    "tenant_slug": {"type": "string"},
                },
                "required": [
                    "id",
                    "principal_kind",
                    "username",
                    "phone",
                    "email",
                    "first_name",
                    "last_name",
                    "middle_name",
                    "full_name",
                    "birthdate",
                    "gender",
                    "preferred_language",
                    "is_active",
                    "must_change_password",
                    "last_login_at",
                    "role_memberships",
                    "effective_permissions",
                    "scopes",
                    "organization_locale",
                    "organization_timezone",
                    "primary_currency",
                    "read_only_session",
                    "session_id",
                    "session_created_at",
                    "session_last_activity_at",
                    "session_expires_at",
                    "session_idle_expires_at",
                    "server_time",
                    "tenant_slug",
                ],
            },
            "UserBootstrapResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {"$ref": "#/components/schemas/UserBootstrapData"},
                },
                "required": ["success", "data"],
            },
            "ExecutiveWindow": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date_from": {"type": "string", "format": "date"},
                    "date_to": {"type": "string", "format": "date"},
                    "timezone": {
                        "type": "string",
                        "description": "Organization IANA timezone used for every boundary.",
                    },
                    "inclusive": {"type": "string", "enum": ["both"]},
                },
                "required": ["date_from", "date_to", "timezone", "inclusive"],
            },
            "ExecutiveBranchLabel": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "integer", "format": "int64", "minimum": 1},
                    "name": {"type": "string"},
                },
                "required": ["id", "name"],
            },
            "ExecutiveDepartmentLabel": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "integer", "format": "int64", "minimum": 1},
                    "name": {"type": "string"},
                    "branch": {"type": "integer", "format": "int64", "minimum": 1},
                },
                "required": ["id", "name", "branch"],
            },
            "ExecutiveAppliedFilters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "branch": {
                        "type": "integer",
                        "format": "int64",
                        "minimum": 1,
                        "nullable": True,
                    },
                    "department": {
                        "type": "integer",
                        "format": "int64",
                        "minimum": 1,
                        "nullable": True,
                    },
                },
                "required": ["branch", "department"],
            },
            "ExecutiveScope": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "organization_wide": {"type": "boolean"},
                    "branches": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/ExecutiveBranchLabel"},
                    },
                    "departments": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/ExecutiveDepartmentLabel"},
                    },
                    "applied_filters": {"$ref": "#/components/schemas/ExecutiveAppliedFilters"},
                },
                "required": ["organization_wide", "branches", "departments", "applied_filters"],
            },
            "ExecutiveStudentMetrics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"type": "integer", "minimum": 0}
                    for name in (
                        "total",
                        "active",
                        "leads",
                        "graduated",
                        "withdrawn",
                        "blocked",
                        "with_cohort",
                        "ungrouped",
                        "joined_in_window",
                    )
                },
                "required": [
                    "total",
                    "active",
                    "leads",
                    "graduated",
                    "withdrawn",
                    "blocked",
                    "with_cohort",
                    "ungrouped",
                    "joined_in_window",
                ],
            },
            "ExecutiveAttendanceMetrics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "attended": {"type": "integer", "minimum": 0},
                    "absent": {"type": "integer", "minimum": 0},
                    "excused": {"type": "integer", "minimum": 0},
                    "denominator": {"type": "integer", "minimum": 0},
                    "attendance_rate_fraction": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "nullable": True,
                    },
                },
                "required": [
                    "attended",
                    "absent",
                    "excused",
                    "denominator",
                    "attendance_rate_fraction",
                ],
            },
            "ExecutiveRetentionMetrics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"type": "integer", "minimum": 0}
                    for name in (
                        "current_student_sample_size",
                        "joined_students",
                        "exited_students",
                        "exit_events",
                    )
                }
                | {
                    "attribution": {
                        "type": "string",
                        "enum": ["current_student_scope"],
                    }
                },
                "required": [
                    "current_student_sample_size",
                    "joined_students",
                    "exited_students",
                    "exit_events",
                    "attribution",
                ],
            },
            "ExecutiveCapacityMetrics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"type": "integer", "minimum": 0}
                    for name in (
                        "active_group_count",
                        "groups_with_declared_capacity",
                        "groups_without_declared_capacity",
                        "declared_seats",
                        "active_students",
                        "active_students_in_measured_groups",
                    )
                }
                | {
                    "seat_balance": {"type": "integer"},
                    "attribution": {
                        "type": "string",
                        "enum": ["current_group_scope"],
                    },
                },
                "required": [
                    "active_group_count",
                    "groups_with_declared_capacity",
                    "groups_without_declared_capacity",
                    "declared_seats",
                    "active_students",
                    "active_students_in_measured_groups",
                    "seat_balance",
                    "attribution",
                ],
            },
            "ExecutiveRiskMetrics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"type": "integer", "minimum": 0}
                    for name in (
                        "student_sample_size",
                        "at_risk_students",
                        "high_risk_students",
                        "medium_risk_students",
                        "low_risk_students",
                        "low_attendance_students",
                        "low_grade_students",
                        "overdue_payment_students",
                    )
                }
                | {
                    "at_risk_rate_fraction": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "nullable": True,
                    },
                    "included_signals": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["low_attendance", "low_grades", "overdue_payment"],
                        },
                        "uniqueItems": True,
                    },
                    "finance_signal_included": {"type": "boolean"},
                },
                "required": [
                    "student_sample_size",
                    "at_risk_students",
                    "high_risk_students",
                    "medium_risk_students",
                    "low_risk_students",
                    "low_attendance_students",
                    "low_grade_students",
                    "overdue_payment_students",
                    "at_risk_rate_fraction",
                    "included_signals",
                    "finance_signal_included",
                ],
            },
            "ExecutiveTeacherMetrics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"type": "integer", "minimum": 0}
                    for name in (
                        "teacher_count",
                        "active_teacher_count",
                        "completed_lessons",
                        "teachers_delivering",
                        "groups_delivered",
                        "attendance_numerator",
                        "attendance_denominator",
                        "students_reached",
                        "lessons_with_attendance",
                        "published_exams_with_results",
                        "graded_results",
                        "assessed_students",
                        "published_exams",
                    )
                }
                | {
                    "attendance_rate_fraction": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "nullable": True,
                    }
                },
                "required": [
                    "teacher_count",
                    "active_teacher_count",
                    "completed_lessons",
                    "teachers_delivering",
                    "groups_delivered",
                    "attendance_numerator",
                    "attendance_denominator",
                    "students_reached",
                    "lessons_with_attendance",
                    "published_exams_with_results",
                    "graded_results",
                    "assessed_students",
                    "published_exams",
                    "attendance_rate_fraction",
                ],
            },
            "ExecutiveTaskAttention": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"type": "integer", "minimum": 0}
                    for name in (
                        "open_assigned_to_me",
                        "blocked_assigned_to_me",
                        "overdue_assigned_to_me",
                    )
                },
                "required": [
                    "open_assigned_to_me",
                    "blocked_assigned_to_me",
                    "overdue_assigned_to_me",
                ],
            },
            "ExecutiveAttention": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tasks": {"$ref": "#/components/schemas/ExecutiveTaskAttention"},
                    "pending_approvals": {"type": "integer", "minimum": 0},
                    "unread_notifications": {"type": "integer", "minimum": 0},
                    "upcoming_meetings": {"type": "integer", "minimum": 0},
                },
            },
            "ExecutiveBranchMetrics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "integer", "format": "int64", "minimum": 1},
                    "name": {"type": "string"},
                    "student_count": {"type": "integer", "minimum": 0},
                    "attendance_numerator": {"type": "integer", "minimum": 0},
                    "attendance_denominator": {"type": "integer", "minimum": 0},
                    "attendance_rate_fraction": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "nullable": True,
                    },
                },
                "required": ["id", "name"],
            },
            "ExecutiveMoney": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "amount_minor": {"type": "integer", "format": "int64", "minimum": 0},
                    "currency": {"type": "string", "enum": ["UZS"]},
                },
                "required": ["amount_minor", "currency"],
            },
            "ExecutiveFinanceMetrics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "billed": {"$ref": "#/components/schemas/ExecutiveMoney"},
                    "collected": {"$ref": "#/components/schemas/ExecutiveMoney"},
                    "outstanding_for_invoices_issued_in_window": {
                        "$ref": "#/components/schemas/ExecutiveMoney"
                    },
                    "overdue_invoice_count": {"type": "integer", "minimum": 0},
                    "refunded": {"$ref": "#/components/schemas/ExecutiveMoney"},
                    "approved_expense": {"$ref": "#/components/schemas/ExecutiveMoney"},
                    "paid_expense": {"$ref": "#/components/schemas/ExecutiveMoney"},
                },
                "required": [
                    "billed",
                    "collected",
                    "outstanding_for_invoices_issued_in_window",
                    "overdue_invoice_count",
                    "refunded",
                ],
            },
            "ExecutiveStudentCoverage": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {"type": "string", "enum": ["complete", "omitted"]},
                    "reason": {"type": "string", "enum": ["insufficient_permission"]},
                    "required_permission": {"type": "string", "enum": ["students:read"]},
                    "sample_size": {"type": "integer", "minimum": 0},
                    "windowed_metrics": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["joined_in_window"]},
                    },
                    "as_of_generated_at": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "total",
                                "active",
                                "leads",
                                "graduated",
                                "withdrawn",
                                "blocked",
                                "with_cohort",
                                "ungrouped",
                            ],
                        },
                    },
                },
                "required": ["status", "required_permission"],
            },
            "ExecutiveAttendanceCoverage": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["complete", "no_data", "omitted"],
                    },
                    "reason": {"type": "string", "enum": ["insufficient_permission"]},
                    "required_permission": {"type": "string", "enum": ["attendance:read"]},
                    "sample_size": {"type": "integer", "minimum": 0},
                    "rate_definition": {"type": "string"},
                },
                "required": ["status", "required_permission"],
            },
            "ExecutiveFinanceWindowBasis": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "billed": {"type": "string", "enum": ["invoice issue_date"]},
                    "collected": {"type": "string", "enum": ["payment paid_at"]},
                    "refunded": {"type": "string", "enum": ["provider_confirmed_at"]},
                    "expenses": {"type": "string", "enum": ["approved_at or paid_at"]},
                },
                "required": ["billed", "collected", "refunded", "expenses"],
            },
            "ExecutiveFinanceCoverage": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["complete", "partial", "omitted"],
                    },
                    "reason": {"type": "string", "enum": ["insufficient_permission"]},
                    "required_permission": {"type": "string", "enum": ["finance:read"]},
                    "currency": {"type": "string", "enum": ["UZS"]},
                    "window_basis": {"$ref": "#/components/schemas/ExecutiveFinanceWindowBasis"},
                    "attribution": {
                        "type": "string",
                        "enum": ["immutable_historical_scope"],
                    },
                    "omitted_metrics": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["approved_expense", "paid_expense"],
                        },
                    },
                },
                "required": ["status", "required_permission"],
            },
            "ExecutiveBranchCoverage": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {"type": "string", "enum": ["complete"]},
                    "derived_from": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["students", "attendance"]},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
                "required": ["status", "derived_from"],
            },
            "ExecutiveSectionCoverage": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["complete", "partial", "no_data", "omitted"],
                    },
                    "reason": {
                        "type": "string",
                        "enum": ["insufficient_permission", "scope_not_representable"],
                    },
                    "required_permission": {"type": "string"},
                    "required_permission_sets": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "uniqueItems": True,
                        },
                        "minItems": 1,
                    },
                    "authorization_basis": {
                        "type": "string",
                        "enum": ["current_principal"],
                    },
                    "sample_size": {"type": "integer", "minimum": 0},
                    "metric_definition": {"type": "string"},
                    "attribution": {"type": "string"},
                    "signals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "uniqueItems": True,
                    },
                },
                "required": ["status"],
            },
            "ExecutiveCoverage": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "students": {"$ref": "#/components/schemas/ExecutiveStudentCoverage"},
                    "attendance": {"$ref": "#/components/schemas/ExecutiveAttendanceCoverage"},
                    "finance": {"$ref": "#/components/schemas/ExecutiveFinanceCoverage"},
                    "branches": {"$ref": "#/components/schemas/ExecutiveBranchCoverage"},
                    "retention": {"$ref": "#/components/schemas/ExecutiveSectionCoverage"},
                    "capacity": {"$ref": "#/components/schemas/ExecutiveSectionCoverage"},
                    "risk": {"$ref": "#/components/schemas/ExecutiveSectionCoverage"},
                    "teachers": {"$ref": "#/components/schemas/ExecutiveSectionCoverage"},
                    "approvals": {"$ref": "#/components/schemas/ExecutiveSectionCoverage"},
                    "tasks": {"$ref": "#/components/schemas/ExecutiveSectionCoverage"},
                    "notifications": {"$ref": "#/components/schemas/ExecutiveSectionCoverage"},
                    "meetings": {"$ref": "#/components/schemas/ExecutiveSectionCoverage"},
                },
                "required": [
                    "students",
                    "attendance",
                    "retention",
                    "capacity",
                    "risk",
                    "teachers",
                    "finance",
                    "approvals",
                    "tasks",
                    "notifications",
                    "meetings",
                ],
            },
            "ExecutiveWarning": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "code": {
                        "type": "string",
                        "enum": [
                            "insufficient_data",
                            "scope_not_representable",
                            "sections_omitted",
                        ],
                    },
                    "message": {"type": "string"},
                    "affected_sections": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "students",
                                "attendance",
                                "retention",
                                "capacity",
                                "risk",
                                "teachers",
                                "finance",
                                "approvals",
                                "tasks",
                                "notifications",
                                "meetings",
                            ],
                        },
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
                "required": ["code", "message", "affected_sections"],
            },
            "ExecutiveSummaryData": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Permission-pruned snapshot. Organization aggregates and personal attention "
                    "panels are absent—not zero-filled—when permission or immutable scope cannot "
                    "be proven. Coverage and warnings explain every omission or partial set."
                ),
                "properties": {
                    "generated_at": {"type": "string", "format": "date-time"},
                    "locale": {"type": "string"},
                    "currency": {"type": "string", "enum": ["UZS"]},
                    "window": {"$ref": "#/components/schemas/ExecutiveWindow"},
                    "scope": {"$ref": "#/components/schemas/ExecutiveScope"},
                    "students": {"$ref": "#/components/schemas/ExecutiveStudentMetrics"},
                    "attendance": {"$ref": "#/components/schemas/ExecutiveAttendanceMetrics"},
                    "retention": {"$ref": "#/components/schemas/ExecutiveRetentionMetrics"},
                    "capacity": {"$ref": "#/components/schemas/ExecutiveCapacityMetrics"},
                    "risk": {"$ref": "#/components/schemas/ExecutiveRiskMetrics"},
                    "teachers": {"$ref": "#/components/schemas/ExecutiveTeacherMetrics"},
                    "branches": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/ExecutiveBranchMetrics"},
                    },
                    "finance": {"$ref": "#/components/schemas/ExecutiveFinanceMetrics"},
                    "attention": {"$ref": "#/components/schemas/ExecutiveAttention"},
                    "coverage": {"$ref": "#/components/schemas/ExecutiveCoverage"},
                    "warnings": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/ExecutiveWarning"},
                    },
                },
                "required": [
                    "generated_at",
                    "locale",
                    "currency",
                    "window",
                    "scope",
                    "coverage",
                    "warnings",
                ],
            },
            "ExecutiveSummaryResponse": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {"$ref": "#/components/schemas/ExecutiveSummaryData"},
                },
                "required": ["success", "data"],
            },
            "UserProfileUpdateRequest": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "first_name": {"type": "string", "maxLength": 150},
                    "last_name": {"type": "string", "maxLength": 150},
                    "middle_name": {"type": "string", "maxLength": 150},
                    "phone": {"type": "string", "maxLength": 32, "nullable": True},
                    "email": {
                        "type": "string",
                        "format": "email",
                        "maxLength": 254,
                        "nullable": True,
                    },
                    "birthdate": {"type": "string", "format": "date", "nullable": True},
                    "gender": {"type": "string", "enum": ["", "m", "f"]},
                    "preferred_language": {"type": "string", "enum": ["uz", "ru", "en"]},
                },
                "description": (
                    "Only supplied fields are changed. Identity and preferred_language updates "
                    "apply to both role-native and legacy profiles; unsupported fields are rejected."
                ),
            },
            "PasswordChangeError": {
                "anyOf": [
                    {"$ref": "#/components/schemas/WrongPasswordError"},
                    {"$ref": "#/components/schemas/WeakPasswordError"},
                    {"$ref": "#/components/schemas/Error"},
                ]
            },
            "PayoutPolicyRequest": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": [
                            "hourly",
                            "percent_of_collected_tuition",
                            "flat_monthly",
                        ],
                    },
                    "hourly_rate_uzs": {
                        "type": "string",
                        "pattern": r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$",
                    },
                    "flat_amount_uzs": {
                        "type": "string",
                        "pattern": r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$",
                    },
                    "tuition_percent": {
                        "type": "string",
                        "pattern": r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$",
                    },
                    "is_active": {"type": "boolean", "default": True},
                },
                "required": ["method"],
                "oneOf": [
                    {
                        "properties": {"method": {"enum": ["hourly"]}},
                        "required": ["hourly_rate_uzs"],
                    },
                    {
                        "properties": {"method": {"enum": ["percent_of_collected_tuition"]}},
                        "required": ["tuition_percent"],
                    },
                    {
                        "properties": {"method": {"enum": ["flat_monthly"]}},
                        "required": ["flat_amount_uzs"],
                    },
                ],
            },
            "PayoutPolicy": {
                "type": "object",
                "properties": {
                    "teacher": {"type": "integer", "format": "int64"},
                    "method": {
                        "type": "string",
                        "enum": [
                            "hourly",
                            "percent_of_collected_tuition",
                            "flat_monthly",
                        ],
                    },
                    "hourly_rate_uzs": {"type": "string", "nullable": True},
                    "flat_amount_uzs": {"type": "string", "nullable": True},
                    "tuition_percent": {"type": "string", "nullable": True},
                    "is_active": {"type": "boolean"},
                    "updated_at": {"type": "string", "format": "date-time"},
                },
                "required": [
                    "teacher",
                    "method",
                    "hourly_rate_uzs",
                    "flat_amount_uzs",
                    "tuition_percent",
                    "is_active",
                    "updated_at",
                ],
            },
            "PayoutPolicyResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {"$ref": "#/components/schemas/PayoutPolicy"},
                },
                "required": ["success", "data"],
            },
            "SalaryPrepareRequest": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "period_start": {"type": "string", "format": "date"},
                    "period_end": {"type": "string", "format": "date"},
                },
                "required": ["period_start", "period_end"],
            },
            "SalaryPrepareData": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "integer", "format": "int64"},
                    "kind": {"type": "string", "enum": ["salary_prep"]},
                    "amount_uzs": {
                        "type": "string",
                        "description": "Legacy decimal-major UZS field; never a JSON float.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "approved", "rejected", "disbursed", "cancelled"],
                    },
                    "breakdown": {"type": "object"},
                },
                "required": ["request_id", "kind", "amount_uzs", "status", "breakdown"],
            },
            "SalaryPrepareResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {"$ref": "#/components/schemas/SalaryPrepareData"},
                },
                "required": ["success", "data"],
            },
            "PrintJobCreateRequest": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Identifies a printable domain record. payload_s3_key, branch, cohort, "
                    "status, printer, and agent are server-owned and rejected if supplied."
                ),
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["assignment", "transcript", "report", "receipt"],
                    },
                    "source_id": {
                        "type": "integer",
                        "format": "int64",
                        "minimum": 1,
                        "description": (
                            "Assignment, Transcript, ReportRun, or Payment ID according to source."
                        ),
                    },
                    "attachment_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Required only when an assignment has multiple attachments; zero-based."
                        ),
                    },
                    "pages": {"type": "integer", "minimum": 1},
                    "copies": {"type": "integer", "minimum": 1, "default": 1},
                    "color": {"type": "boolean", "default": False},
                    "duplex": {"type": "boolean", "default": False},
                },
                "required": ["source", "source_id", "pages"],
            },
            "PrintJob": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Leadership-safe operational state. Storage locations and internal device "
                    "errors are intentionally absent."
                ),
                "properties": {
                    "id": {"type": "integer"},
                    "branch": {"type": "integer"},
                    "printer": {"type": "integer", "nullable": True},
                    "agent": {"type": "integer", "nullable": True},
                    "status": {
                        "type": "string",
                        "enum": [
                            "queued",
                            "picked",
                            "printing",
                            "reconciliation_required",
                            "done",
                            "failed",
                        ],
                    },
                    "source": {
                        "type": "string",
                        "enum": ["assignment", "transcript", "report", "receipt"],
                    },
                    "source_id": {"type": "integer", "format": "int64"},
                    "pages": {"type": "integer"},
                    "copies": {"type": "integer"},
                    "color": {"type": "boolean"},
                    "duplex": {"type": "boolean"},
                    "cohort_id": {"type": "integer", "format": "int64", "nullable": True},
                    "requested_by": {"type": "integer", "nullable": True},
                    "attempts": {"type": "integer"},
                    "next_attempt_at": {"type": "string", "format": "date-time", "nullable": True},
                    "pages_printed": {"type": "integer"},
                    "created_at": {"type": "string", "format": "date-time"},
                    "claimed_at": {"type": "string", "format": "date-time", "nullable": True},
                    "last_heartbeat_at": {
                        "type": "string",
                        "format": "date-time",
                        "nullable": True,
                    },
                    "lease_expires_at": {
                        "type": "string",
                        "format": "date-time",
                        "nullable": True,
                    },
                    "reconciliation_required_at": {
                        "type": "string",
                        "format": "date-time",
                        "nullable": True,
                    },
                    "reconciliation_reason": {
                        "type": "string",
                        "enum": [
                            "lease_expired",
                            "legacy_unleased",
                            "agent_reported_failure",
                        ],
                        "nullable": True,
                    },
                    "reconciliation_previous_status": {
                        "type": "string",
                        "enum": ["picked", "printing"],
                        "nullable": True,
                    },
                    "finished_at": {"type": "string", "format": "date-time", "nullable": True},
                },
                "required": [
                    "id",
                    "branch",
                    "printer",
                    "agent",
                    "status",
                    "source",
                    "source_id",
                    "pages",
                    "copies",
                    "color",
                    "duplex",
                    "cohort_id",
                    "requested_by",
                    "attempts",
                    "next_attempt_at",
                    "pages_printed",
                    "created_at",
                    "claimed_at",
                    "last_heartbeat_at",
                    "lease_expires_at",
                    "reconciliation_required_at",
                    "reconciliation_reason",
                    "reconciliation_previous_status",
                    "finished_at",
                ],
            },
            "PrintJobReconcileRequest": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "confirmed_printed",
                            "confirmed_not_printed",
                            "abandoned_unknown",
                        ],
                    },
                    "evidence_reference": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                        "description": (
                            "Reviewed incident, printer-queue, or physical-inspection reference."
                        ),
                    },
                },
                "required": ["outcome", "evidence_reference"],
            },
            "PrintJobReconciliation": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Append-only reviewed evidence. Raw lease UUIDs and idempotency keys are omitted."
                ),
                "properties": {
                    "id": {"type": "integer", "format": "int64", "minimum": 1},
                    "job": {"type": "integer", "format": "int64", "minimum": 1},
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "confirmed_printed",
                            "confirmed_not_printed",
                            "abandoned_unknown",
                        ],
                    },
                    "evidence_reference": {"type": "string", "maxLength": 200},
                    "previous_status": {"type": "string", "enum": ["picked", "printing"]},
                    "reason": {
                        "type": "string",
                        "enum": [
                            "lease_expired",
                            "legacy_unleased",
                            "agent_reported_failure",
                        ],
                    },
                    "pages_printed": {"type": "integer", "minimum": 0},
                    "attempts": {"type": "integer", "minimum": 0},
                    "agent": {"type": "integer", "format": "int64", "nullable": True},
                    "printer": {"type": "integer", "format": "int64", "nullable": True},
                    "resolved_by": {"type": "integer", "format": "int64", "nullable": True},
                    "resolved_at": {"type": "string", "format": "date-time"},
                },
                "required": [
                    "id",
                    "job",
                    "outcome",
                    "evidence_reference",
                    "previous_status",
                    "reason",
                    "pages_printed",
                    "attempts",
                    "agent",
                    "printer",
                    "resolved_by",
                    "resolved_at",
                ],
            },
            "PrintJobResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {"$ref": "#/components/schemas/PrintJob"},
                },
                "required": ["success", "data"],
            },
            "PrintJobPageResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/PrintJob"},
                    },
                    "pagination": {"$ref": "#/components/schemas/Pagination"},
                },
                "required": ["success", "data", "pagination"],
            },
            "PrintJobReconciliationPageResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/PrintJobReconciliation"},
                    },
                    "pagination": {"$ref": "#/components/schemas/Pagination"},
                },
                "required": ["success", "data", "pagination"],
            },
            "Wallet": {
                "type": "object",
                "properties": {
                    "student": {"type": "integer"},
                    "balance_uzs": {
                        "type": "string",
                        "pattern": r"^-?\d+\.\d{2}$",
                        "description": "Legacy decimal-major UZS field; never a JSON float.",
                    },
                    "updated_at": {"type": "string", "format": "date-time"},
                },
                "required": ["student", "balance_uzs", "updated_at"],
            },
            "WalletTransaction": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "kind": {"type": "string"},
                    "amount_uzs": {"type": "string", "pattern": r"^-?\d+\.\d{2}$"},
                    "balance_after_uzs": {
                        "type": "string",
                        "pattern": r"^-?\d+\.\d{2}$",
                    },
                    "created_by": {"type": "integer", "nullable": True},
                    "note": {"type": "string"},
                    "created_at": {"type": "string", "format": "date-time"},
                },
                "required": [
                    "id",
                    "kind",
                    "amount_uzs",
                    "balance_after_uzs",
                    "created_by",
                    "note",
                    "created_at",
                ],
            },
            "WalletPayloadData": {
                "type": "object",
                "properties": {
                    "wallet": {
                        "nullable": True,
                        "allOf": [{"$ref": "#/components/schemas/Wallet"}],
                        "description": "Null when no wallet has been provisioned by a write workflow.",
                    },
                    "transactions": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/WalletTransaction"},
                    },
                },
                "required": ["wallet", "transactions"],
            },
            "WalletPayloadResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "enum": [True]},
                    "data": {"$ref": "#/components/schemas/WalletPayloadData"},
                },
                "required": ["success", "data"],
            },
            **workflow_schemas,
        },
    }
    runtime_warnings = {
        "type": "array",
        "items": {"$ref": "#/components/schemas/RuntimeWarning"},
        "description": "Present when an optional dependency is degraded.",
    }
    for schema in components["schemas"].values():
        if not isinstance(schema, dict) or schema.get("type") != "object":
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict) or "warnings" in properties:
            continue
        success_schema = properties.get("success")
        if not isinstance(success_schema, dict):
            continue
        if success_schema.get("enum") == [True] or success_schema.get("example") is True:
            properties["warnings"] = deepcopy(runtime_warnings)
    return components


@lru_cache(maxsize=8)
def _build_paths(urlconf: str | None) -> tuple[dict, tuple[str, ...]]:
    """The ``paths`` object + sorted tag list for a URLconf (cached — source is static)."""
    paths: dict[str, dict] = {}
    tags: set[str] = set()
    for route, callback, name in _walk(get_resolver(urlconf).url_patterns, ""):
        if not route.startswith(_API_PREFIX):
            continue
        if getattr(getattr(callback, "cls", None), "__name__", "") == "APIRootView":
            continue  # DRF DefaultRouter's api-root listing — not a real resource endpoint
        try:
            path, params = _openapi_path(route)
            contract = get_openapi_contract(callback)
            if contract is not None:
                _validate_view_contract(
                    contract=contract,
                    callback=callback,
                    path=path,
                    name=name,
                )
                methods = [operation.method for operation in contract.operations]
                perm = None
            else:
                methods, perm, _resource = _methods_and_meta(callback)
        except OpenAPIContractError:
            # A registered critical operation is a release contract. Silently
            # dropping it would make the schema look valid while hiding drift.
            raise
        except Exception as exc:
            # A partially generated schema is more dangerous than a failed
            # schema build: omitted operations look intentionally unsupported
            # to generated clients and release checks. Preserve the route in
            # the diagnostic while avoiding request/runtime values.
            raise OpenAPIContractError(f"Unable to describe registered route {route!r}") from exc
        tags.add(_tag_for(path))
        item = paths.setdefault(path, {})
        if params:
            item.setdefault("parameters", params)
        if contract is not None:
            for operation in contract.operations:
                key = operation.method.lower()
                if key in item:
                    raise OpenAPIContractError(f"Duplicate explicit {operation.method} operation for {path}")
                item[key] = _contract_operation(contract=operation, name=name, path=path)
        else:
            public = _is_public(path)
            for method in methods:
                op = _operation(method=method, name=name, path=path, perm=perm, public=public)
                item[method.lower()] = op
    return paths, tuple(sorted(tags))


def build_schema(urlconf: str | None, *, base_url: str = "") -> dict:
    """A complete OpenAPI 3.0.3 document for ``urlconf`` (the active tenant/public URLconf)."""
    paths, tags = _build_paths(urlconf)
    doc: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": "Starforge Edu API",
            "version": "1.0.0",
            "description": (
                "Multi-tenant education-center platform API. Every response uses one envelope: "
                "success `{success:true, data, pagination?}`, error `{success:false, code, message, errors?}`. "
                "Authentication uses an opaque session key through Bearer or a Secure HttpOnly cookie; "
                "cookie-authenticated unsafe methods also require the CSRF header. "
                "The tenant is selected by the request Host (a center subdomain); a mobile app resolves it "
                "first via `GET /api/v1/platform/resolve/?slug=<center>`. See `agents/API-CONTRACT.md` for "
                "the full narrative and field-level detail."
            ),
        },
        "paths": paths,
        "components": _components(),
        "tags": [{"name": t} for t in tags],
    }
    if base_url:
        doc["servers"] = [{"url": base_url}]
    return doc


def openapi_schema_view(request: HttpRequest) -> JsonResponse:
    """Serve the generated OpenAPI 3.0 document for the ACTIVE URLconf.

    On a tenant host ``request.urlconf`` is unset → the tenant API (``config.urls``); on the
    public/apex host django-tenants sets it to ``config.urls_public`` → the platform API. So one
    view serves the right schema per host. Public (no auth) so a client dev / codegen tool can
    fetch it without a token; Swagger UI + Redoc fetch this URL client-side.
    """
    urlconf = getattr(request, "urlconf", None)
    base_url = f"{request.scheme}://{request.get_host()}"
    response = JsonResponse(build_schema(urlconf, base_url=base_url))
    response["Access-Control-Allow-Origin"] = "*"  # the schema is public API metadata
    return response
