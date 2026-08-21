#!/usr/bin/env python3
"""Run privacy-safe leadership HTTP contract smoke against a private candidate.

Credentials and tenant fixture IDs come from one root-only JSON bind mount. The
result contains operation names, statuses, and hashes only; bearer credentials
and response bodies are never printed. Public ingress remains in maintenance
while this process connects directly to the candidate web container.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import requests

_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{0,62}\Z")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{2,95}\Z")
_MINIMUM_CATALOG_OPERATIONS = 104
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class SmokeFailure(RuntimeError):
    """A deliberately non-sensitive release-smoke failure."""


@dataclass(frozen=True)
class RoleSession:
    name: str
    access: str
    membership: str
    effective_permissions: frozenset[str]
    scope_count: int


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _allows(grants: frozenset[str], permission: str) -> bool:
    resource, separator, _verb = permission.partition(":")
    return "*:*" in grants or permission in grants or (bool(separator) and f"{resource}:*" in grants)


def _pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("JSON pointers must be empty or begin with '/'")
    current = document
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(pointer)
    return current


def _validate_path(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/api/v1/"):
        raise ValueError("catalog paths must stay below /api/v1/")
    parsed = urlsplit(value)
    decoded = unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or "\\" in value
        or "//" in parsed.path
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise ValueError("catalog path is not normalized")
    return value


def validate_config(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "version",
        "tenant_host",
        "director",
        "manager",
        "manager_out_of_scope_user_id",
        "catalog",
    }:
        raise ValueError("smoke configuration has an unexpected top-level shape")
    if document["version"] != 1:
        raise ValueError("unsupported smoke configuration version")
    host = document["tenant_host"]
    if not isinstance(host, str) or _HOST.fullmatch(host.lower()) is None:
        raise ValueError("tenant_host must be one normalized DNS hostname")
    document["tenant_host"] = host.lower()

    for role in ("director", "manager"):
        account = document[role]
        if not isinstance(account, dict) or set(account) != {"username", "password"}:
            raise ValueError(f"{role} credentials have an unexpected shape")
        username = account["username"]
        password = account["password"]
        if not isinstance(username, str) or not 1 <= len(username) <= 150 or username.strip() != username:
            raise ValueError(f"{role} username is invalid")
        if not isinstance(password, str) or not 10 <= len(password) <= 128 or password.startswith("REPLACE_"):
            raise ValueError(f"{role} smoke password is invalid or still a placeholder")
    out_of_scope_id = document["manager_out_of_scope_user_id"]
    if not isinstance(out_of_scope_id, int) or isinstance(out_of_scope_id, bool) or out_of_scope_id <= 0:
        raise ValueError("manager_out_of_scope_user_id must be a positive integer")

    catalog = document["catalog"]
    if not isinstance(catalog, list) or len(catalog) < _MINIMUM_CATALOG_OPERATIONS:
        raise ValueError(
            f"catalog must contain at least {_MINIMUM_CATALOG_OPERATIONS} executable leadership reads"
        )
    names: set[str] = set()
    operations: set[tuple[str, str]] = set()
    for index, operation in enumerate(catalog):
        if not isinstance(operation, dict) or set(operation) - {
            "name",
            "role",
            "path",
            "expected_status",
            "required_json_pointers",
            "forbidden_json_pointers",
        }:
            raise ValueError(f"catalog operation {index} has unsupported fields")
        name = operation.get("name")
        role = operation.get("role")
        path = _validate_path(operation.get("path"))
        if not isinstance(name, str) or _NAME.fullmatch(name) is None or name in names:
            raise ValueError(f"catalog operation {index} has an invalid or duplicate name")
        if role not in {"director", "manager"}:
            raise ValueError(f"catalog operation {name} has an invalid role")
        identity = (role, path)
        if identity in operations:
            raise ValueError(f"catalog operation {name} duplicates a role/path read")
        names.add(name)
        operations.add(identity)
        operation["path"] = path
        status = operation.get("expected_status", 200)
        if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status <= 499:
            raise ValueError(f"catalog operation {name} has an invalid expected status")
        operation["expected_status"] = status
        for field in ("required_json_pointers", "forbidden_json_pointers"):
            pointers = operation.get(field, [])
            if not isinstance(pointers, list) or not all(
                isinstance(pointer, str) and (pointer == "" or pointer.startswith("/"))
                for pointer in pointers
            ):
                raise ValueError(f"catalog operation {name} has invalid {field}")
            operation[field] = pointers
    return document


def load_config(path: Path, *, require_private: bool) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("smoke configuration is not a regular file")
    if require_private:
        metadata = path.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
            raise ValueError("smoke configuration must be root-owned with mode 0400 or 0600")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("smoke configuration is not valid UTF-8 JSON") from exc
    return validate_config(document)


def _json_response(response: requests.Response, *, operation: str) -> dict[str, Any]:
    content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/json" or len(response.content) > _MAX_RESPONSE_BYTES:
        raise SmokeFailure(f"{operation}: response contract is not bounded JSON")
    try:
        document = response.json()
    except requests.JSONDecodeError as exc:
        raise SmokeFailure(f"{operation}: response is not JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("success"), bool):
        raise SmokeFailure(f"{operation}: response has no canonical success envelope")
    return document


def _request(
    session: requests.Session,
    *,
    base_url: str,
    host: str,
    method: str,
    path: str,
    operation: str,
    expected_status: int,
    access: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[requests.Response, dict[str, Any]]:
    headers = {
        "Host": host,
        "Accept": "application/json",
        "User-Agent": "starforge-release-smoke/1",
        # The smoke connects directly to Gunicorn while exercising the same
        # secure-origin contract used behind Caddy. Without this trusted proxy
        # marker Django correctly redirects the HTTP hop to public HTTPS.
        "X-Forwarded-Proto": "https",
    }
    if access:
        headers["Authorization"] = f"Bearer {access}"
    try:
        response = session.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            json=body,
            timeout=(3.05, 15),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise SmokeFailure(f"{operation}: private candidate request failed") from exc
    document = _json_response(response, operation=operation)
    if response.status_code != expected_status:
        raise SmokeFailure(f"{operation}: expected HTTP {expected_status}, received {response.status_code}")
    if 200 <= expected_status < 300 and document["success"] is not True:
        raise SmokeFailure(f"{operation}: successful status carried a failure envelope")
    if expected_status >= 400 and document["success"] is not False:
        raise SmokeFailure(f"{operation}: failure status carried a success envelope")
    return response, document


def _login(
    session: requests.Session,
    *,
    base_url: str,
    host: str,
    role_name: str,
    credentials: dict[str, str],
    revision: str,
) -> RoleSession:
    _, login = _request(
        session,
        base_url=base_url,
        host=host,
        method="POST",
        path="/api/v1/auth/role-login/",
        operation=f"{role_name}-role-login",
        expected_status=200,
        body={
            "username": credentials["username"],
            "password": credentials["password"],
            "device_id": f"release-smoke-{revision[:20]}",
            "platform": "web",
        },
    )
    login_data = login.get("data")
    access = login_data.get("access") if isinstance(login_data, dict) else None
    if not isinstance(access, str) or not access or login_data.get("must_change_password") is not False:
        raise SmokeFailure(f"{role_name}-role-login: unusable session contract")
    _, me = _request(
        session,
        base_url=base_url,
        host=host,
        method="GET",
        path="/api/v1/users/me/",
        operation=f"{role_name}-identity-bootstrap",
        expected_status=200,
        access=access,
    )
    data = me.get("data")
    memberships = data.get("role_memberships") if isinstance(data, dict) else None
    permissions = data.get("effective_permissions") if isinstance(data, dict) else None
    scopes = data.get("scopes") if isinstance(data, dict) else None
    required_membership = "director" if role_name == "director" else "head_of_dept"
    slugs = {row.get("account_type_slug") for row in memberships or [] if isinstance(row, dict)}
    if (
        data.get("principal_kind") != "staff"
        or required_membership not in slugs
        or not isinstance(permissions, list)
        or not all(isinstance(permission, str) for permission in permissions)
        or not isinstance(scopes, list)
        or data.get("read_only_session") is not False
        or not isinstance(data.get("organization_timezone"), str)
        or not isinstance(data.get("primary_currency"), str)
    ):
        raise SmokeFailure(f"{role_name}-identity-bootstrap: incomplete authorization contract")
    if role_name == "manager" and not scopes:
        raise SmokeFailure("manager-identity-bootstrap: scoped manager has no authoritative scope")
    return RoleSession(
        name=role_name,
        access=access,
        membership=required_membership,
        effective_permissions=frozenset(permissions),
        scope_count=len(scopes),
    )


def run_smoke(config: dict[str, Any], *, base_url: str, revision: str) -> dict[str, Any]:
    session = requests.Session()
    roles: dict[str, RoleSession] = {}
    results: list[dict[str, Any]] = []
    logout_failed = False
    primary_failure: Exception | None = None
    try:
        for role_name in ("director", "manager"):
            roles[role_name] = _login(
                session,
                base_url=base_url,
                host=config["tenant_host"],
                role_name=role_name,
                credentials=config[role_name],
                revision=revision,
            )

        out_id = config["manager_out_of_scope_user_id"]
        for role_name, expected_status in (("manager", 404), ("director", 200)):
            _, document = _request(
                session,
                base_url=base_url,
                host=config["tenant_host"],
                method="GET",
                path=f"/api/v1/users/{out_id}/",
                operation=f"{role_name}-cross-scope-user-detail",
                expected_status=expected_status,
                access=roles[role_name].access,
            )
            if expected_status == 404 and document.get("code") != "not_found":
                raise SmokeFailure("manager-cross-scope-user-detail: existence did not fail closed")
            results.append({"name": f"{role_name}-cross-scope-user-detail", "status": expected_status})

        for role_name in ("director", "manager"):
            _, summary = _request(
                session,
                base_url=base_url,
                host=config["tenant_host"],
                method="GET",
                path="/api/v1/intelligence/executive-summary/",
                operation=f"{role_name}-executive-summary",
                expected_status=200,
                access=roles[role_name].access,
            )
            data = summary.get("data")
            if not isinstance(data, dict) or not all(
                field in data for field in ("generated_at", "scope", "coverage", "warnings")
            ):
                raise SmokeFailure(f"{role_name}-executive-summary: snapshot metadata is incomplete")
            should_have_finance = _allows(roles[role_name].effective_permissions, "finance:read")
            if ("finance" in data) is not should_have_finance:
                raise SmokeFailure(f"{role_name}-executive-summary: finance permission pruning is incorrect")
            results.append({"name": f"{role_name}-executive-summary", "status": 200})

        for operation in config["catalog"]:
            role = roles[operation["role"]]
            response, document = _request(
                session,
                base_url=base_url,
                host=config["tenant_host"],
                method="GET",
                path=operation["path"],
                operation=operation["name"],
                expected_status=operation["expected_status"],
                access=role.access,
            )
            for pointer in operation["required_json_pointers"]:
                try:
                    _pointer(document, pointer)
                except KeyError as exc:
                    raise SmokeFailure(f"{operation['name']}: required response field is absent") from exc
            for pointer in operation["forbidden_json_pointers"]:
                try:
                    _pointer(document, pointer)
                except KeyError:
                    pass
                else:
                    raise SmokeFailure(f"{operation['name']}: forbidden response field is present")
            results.append({"name": operation["name"], "status": response.status_code})
    except Exception as exc:
        primary_failure = exc
    finally:
        for role in roles.values():
            try:
                response = session.post(
                    f"{base_url}/api/v1/auth/logout/",
                    headers={
                        "Host": config["tenant_host"],
                        "Authorization": f"Bearer {role.access}",
                        "Accept": "application/json",
                        "X-Forwarded-Proto": "https",
                    },
                    json={},
                    timeout=(3.05, 15),
                    allow_redirects=False,
                )
                document = _json_response(response, operation=f"{role.name}-logout")
                if response.status_code != 200 or document["success"] is not True:
                    logout_failed = True
            except (requests.RequestException, SmokeFailure):
                logout_failed = True
        session.close()

    if logout_failed:
        raise SmokeFailure("leadership-logout: session cleanup failed") from primary_failure
    if primary_failure is not None:
        raise primary_failure

    return {
        "revision": revision,
        "tenant_host_sha256": _sha256(config["tenant_host"]),
        "roles": {
            name: {
                "membership": role.membership,
                "permission_set_sha256": _sha256("\0".join(sorted(role.effective_permissions))),
                "scope_count": role.scope_count,
            }
            for name, role in roles.items()
        },
        "operation_count": len(results),
        "operations": results,
        "passed": True,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if _REVISION.fullmatch(args.expected_revision) is None:
        print("Leadership smoke requires an exact 40-character revision.", file=sys.stderr)
        return 2
    parsed_base = urlsplit(args.base_url)
    if (
        parsed_base.scheme != "http"
        or parsed_base.hostname not in {"127.0.0.1", "localhost"}
        or parsed_base.port != 8000
        or parsed_base.path not in {"", "/"}
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.query
        or parsed_base.fragment
    ):
        print("Leadership smoke base URL must be private candidate HTTP on port 8000.", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config, require_private=True)
        if args.validate_only:
            print(
                json.dumps(
                    {
                        "revision": args.expected_revision,
                        "catalog_operations": len(config["catalog"]),
                        "valid": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        result = run_smoke(config, base_url=args.base_url.rstrip("/"), revision=args.expected_revision)
    except (OSError, ValueError, SmokeFailure) as exc:
        print(f"Leadership release smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
