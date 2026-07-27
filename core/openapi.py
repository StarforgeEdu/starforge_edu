"""Custom OpenAPI 3.0 schema builder for the off-DRF layered API.

`drf-spectacular` only introspects DRF views (APIViews / ViewSets). 37 of the 38 apps are plain
Django function views, so the auto-generated schema covered only the lone remaining DRF app
(`reports`) — Swagger showed ~5 of ~320 endpoints and no client could generate a typed SDK.

This builder walks the active URLconf and, for every ``/api/v1/`` endpoint, derives:

* the OpenAPI **path** (Django ``<int:pk>`` → ``{pk}``) + typed path parameters,
* the HTTP **methods** the view handles (introspected from its ``request.method`` branches —
  the layered views branch on the method internally, so this is read straight from source),
* the required **permission** (from ``check_perm(request, "resource:action")``),
* **auth** (every endpoint needs the ``Authorization: Bearer <session-key>`` scheme except a
  small allowlist of public routes — login, password reset, tenant resolve, webhooks),
* the project's **standard response envelope** (success / paginated / flat error).

The result is a complete, valid OpenAPI 3.0.3 document that Swagger UI / Redoc render and that
`openapi-generator` / `swagger-codegen` can turn into a TypeScript or Dart client. It is built
once per process per URLconf (source never changes at runtime) and cached.
"""

from __future__ import annotations

import inspect
import re
from functools import lru_cache
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.urls import URLPattern, URLResolver, get_resolver

_API_PREFIX = "api/v1/"

# Public (unauthenticated) endpoints — everything else requires the session-key Bearer scheme.
# Matched as a substring of the OpenAPI path (leading slash included).
_PUBLIC_PATH_MARKERS = (
    "/api/v1/auth/login",
    "/api/v1/auth/password/reset/request",
    "/api/v1/auth/password/reset/confirm",
    "/api/v1/platform/resolve",
    "/api/v1/webhooks/",
)

_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_METHOD_RE = re.compile(r'"(GET|POST|PUT|PATCH|DELETE)"')
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
        params.append(
            {"name": name, "in": "path", "required": True, "schema": schema}
        )
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


def _tag_for(path: str) -> str:
    """Group operations by their mount (``/api/v1/<mount>/...`` -> ``mount``)."""
    parts = [p for p in path.split("/") if p and p not in ("api", "v1")]
    return parts[0] if parts else "root"


def _summary(name: str, method: str, path: str) -> str:
    if name:
        return f"{method} {name.replace('-', ' ')}"
    return f"{method} {path}"


def _is_public(path: str) -> bool:
    return any(marker in path for marker in _PUBLIC_PATH_MARKERS)


def _operation(*, method: str, name: str, path: str, perm: str | None, public: bool) -> dict:
    op: dict[str, Any] = {
        "operationId": f"{method.lower()}_{name or _tag_for(path)}".replace("-", "_"),
        "summary": _summary(name, method, path),
        "tags": [_tag_for(path)],
        "responses": _responses(method),
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
            {"name": "ordering", "in": "query", "schema": {"type": "string"},
             "description": "Field to sort by; prefix with `-` for descending."},
            {"name": "search", "in": "query", "schema": {"type": "string"}},
        ]
    return op


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
    return {
        "securitySchemes": {
            "sessionAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Opaque session key from `POST /api/v1/auth/login/`, sent as "
                    "`Authorization: Bearer <key>`. Hard 7-day expiry; on 401 re-login."
                ),
            }
        },
        "schemas": {
            "Success": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "example": True},
                    "data": {"description": "Endpoint-specific payload (object or array)."},
                    "pagination": {"$ref": "#/components/schemas/Pagination"},
                    "warnings": {
                        "type": "array", "items": {"type": "string"},
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
                    "code": {"type": "string", "description": "Stable, machine-branchable error code.",
                             "example": "validation_error"},
                    "message": {"type": "string", "description": "Human-readable (localized) detail."},
                    "errors": {"type": "object", "description": "Optional per-field validation errors."},
                },
                "required": ["success", "code", "message"],
            },
        },
    }


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
            methods, perm, _resource = _methods_and_meta(callback)
        except Exception:  # never let one odd view break the whole schema
            continue
        public = _is_public(path)
        tags.add(_tag_for(path))
        item = paths.setdefault(path, {})
        if params:
            item.setdefault("parameters", params)
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
                "Auth is an opaque session-key Bearer token (see the `sessionAuth` scheme). "
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
