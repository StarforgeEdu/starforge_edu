"""Parameterized permission matrix (TASKS §3, §26 item 8).

Declarative MATRIX_CASES — Day-2+ lanes append one tuple per case. Drives the
fail-closed (TD-4) per-action (TD-5) permission system against real endpoints.
"""

from types import SimpleNamespace

import pytest
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from core.permissions import Role, RolePermission

pytestmark = pytest.mark.django_db


def _err_code(resp) -> str | None:
    """The stable error code from either envelope: layered plain views return
    ``{"code": ...}``; still-DRF endpoints return ``{"error": {"code": ...}}``.
    The matrix spans both during the off-DRF migration."""
    body = resp.json()
    return body["code"] if "code" in body else body.get("error", {}).get("code")

# (role, http_method, url, allowed?) — `allowed=True` expects 200, else 403.
MATRIX_CASES = [
    # users directory (users:read)
    (Role.DIRECTOR, "get", "/api/v1/users/", True),
    (Role.IT, "get", "/api/v1/users/", True),
    (Role.STUDENT, "get", "/api/v1/users/", False),
    # org branches (org:read / org:write) — teacher GET 200 per D1-LF-8.
    (Role.DIRECTOR, "get", "/api/v1/org/branches/", True),
    (Role.IT, "get", "/api/v1/org/branches/", True),
    (Role.TEACHER, "get", "/api/v1/org/branches/", True),
    (Role.TEACHER, "post", "/api/v1/org/branches/", False),
    # org settings singleton (org:read) — teacher GET 200 per D1-LB-3.
    (Role.DIRECTOR, "get", "/api/v1/org/settings/", True),
    (Role.TEACHER, "get", "/api/v1/org/settings/", True),
    # students (students:read) — parent/student self-service reads are scoped
    # by selectors (TD-5); the gate must let them through.
    (Role.DIRECTOR, "get", "/api/v1/students/", True),
    (Role.REGISTRAR, "get", "/api/v1/students/", True),
    (Role.TEACHER, "get", "/api/v1/students/", True),
    (Role.PARENT, "get", "/api/v1/students/", True),
    (Role.STUDENT, "get", "/api/v1/students/", True),
    (Role.CASHIER, "get", "/api/v1/students/", False),
    # cohorts (cohorts:read)
    (Role.DIRECTOR, "get", "/api/v1/cohorts/", True),
    (Role.TEACHER, "get", "/api/v1/cohorts/", True),
    (Role.CASHIER, "get", "/api/v1/cohorts/", False),
    # teachers (teachers:read)
    (Role.DIRECTOR, "get", "/api/v1/teachers/", True),
    (Role.REGISTRAR, "get", "/api/v1/teachers/", True),
    (Role.STUDENT, "get", "/api/v1/teachers/", False),
    # parents (parents:read) — parent reads own profile via selector scoping.
    (Role.DIRECTOR, "get", "/api/v1/parents/", True),
    (Role.REGISTRAR, "get", "/api/v1/parents/", True),
    (Role.PARENT, "get", "/api/v1/parents/", True),
    (Role.TEACHER, "get", "/api/v1/parents/", False),
    # --- Day 2 ------------------------------------------------------------
    # schedule (schedule:read; REGISTRAR/HEAD/DIRECTOR schedule:*)
    (Role.DIRECTOR, "get", "/api/v1/schedule/lessons/", True),
    (Role.TEACHER, "get", "/api/v1/schedule/lessons/", True),
    (Role.STUDENT, "get", "/api/v1/schedule/lessons/", True),
    (Role.CASHIER, "get", "/api/v1/schedule/lessons/", False),
    (Role.REGISTRAR, "get", "/api/v1/schedule/terms/", True),
    (Role.TEACHER, "post", "/api/v1/schedule/rules/", False),  # teacher has schedule:read only
    (Role.STUDENT, "post", "/api/v1/schedule/rules/", False),
    # attendance (TEACHER/HEAD attendance:*; STUDENT/PARENT attendance:read)
    (Role.DIRECTOR, "get", "/api/v1/attendance/records/", True),
    (Role.TEACHER, "get", "/api/v1/attendance/records/", True),
    (Role.STUDENT, "get", "/api/v1/attendance/records/", True),
    (Role.PARENT, "get", "/api/v1/attendance/records/", True),
    (Role.CASHIER, "get", "/api/v1/attendance/records/", False),
    (Role.STUDENT, "post", "/api/v1/attendance/lessons/1/mark/", False),  # no attendance:write
    # academics (TEACHER academics:read+write; STUDENT/PARENT academics:read)
    (Role.DIRECTOR, "get", "/api/v1/academics/grades/", True),
    (Role.TEACHER, "get", "/api/v1/academics/grades/", True),
    (Role.STUDENT, "get", "/api/v1/academics/grades/", True),
    (Role.PARENT, "get", "/api/v1/academics/grades/", True),
    (Role.CASHIER, "get", "/api/v1/academics/subjects/", False),
    (Role.STUDENT, "post", "/api/v1/academics/subjects/", False),  # no academics:write
    # assignments (TEACHER assignments:*; STUDENT assignments:read+submit)
    (Role.DIRECTOR, "get", "/api/v1/assignments/", True),
    (Role.TEACHER, "get", "/api/v1/assignments/", True),
    (Role.STUDENT, "get", "/api/v1/assignments/", True),
    (Role.CASHIER, "get", "/api/v1/assignments/", False),
    (Role.STUDENT, "post", "/api/v1/assignments/", False),  # create needs assignments:write
    # content (TEACHER/LIBRARIAN content:*; STUDENT content:read)
    (Role.DIRECTOR, "get", "/api/v1/content/libraries/", True),
    (Role.LIBRARIAN, "get", "/api/v1/content/libraries/", True),
    (Role.STUDENT, "get", "/api/v1/content/files/", True),
    (Role.PARENT, "get", "/api/v1/content/files/", True),  # cohort visibility incl. guardian-linked parents
    (Role.CASHIER, "get", "/api/v1/content/files/", False),
    (Role.STUDENT, "post", "/api/v1/content/upload-url/", False),  # needs content:write
    # --- Day 4 · AI (ai:read TEACHER/HEAD; ai:manage director-only; STUDENT/PARENT none)
    (Role.DIRECTOR, "get", "/api/v1/ai/requests/", True),
    (Role.HEAD_OF_DEPT, "get", "/api/v1/ai/requests/", True),
    (Role.TEACHER, "get", "/api/v1/ai/requests/", True),
    (Role.STUDENT, "get", "/api/v1/ai/requests/", False),
    (Role.PARENT, "get", "/api/v1/ai/requests/", False),
    (Role.CASHIER, "get", "/api/v1/ai/requests/", False),
    (Role.TEACHER, "get", "/api/v1/ai/budget/", True),
    (Role.STUDENT, "get", "/api/v1/ai/budget/", False),
    (Role.TEACHER, "patch", "/api/v1/ai/budget/", False),  # ai:manage director-only
]


@pytest.mark.parametrize(("role", "method", "url", "allowed"), MATRIX_CASES)
def test_permission_matrix(as_role, role, method, url, allowed):
    client, _ = as_role(role)
    resp = getattr(client, method)(url)
    if allowed:
        assert resp.status_code == 200, f"{role} {method} {url} -> {resp.status_code}"
    else:
        assert resp.status_code == 403, f"{role} {method} {url} -> {resp.status_code}"
        assert _err_code(resp) == "forbidden"


def test_role_without_code_denied(as_role):
    """A role lacking the required permission code is denied (ordinary matrix
    denial). The true TD-4 fail-closed cases are the unit tests below."""
    client, _ = as_role(Role.SECURITY)
    assert client.get("/api/v1/parents/").status_code == 403


def _authed_request(method="get"):
    req = Request(getattr(APIRequestFactory(), method)("/synthetic/"))
    req.user = SimpleNamespace(is_authenticated=True, is_superuser=False)  # type: ignore[assignment]
    return req


def test_fail_closed_no_mapping_403():
    """TD-4 (DAY-1 Lane C): a view declaring neither `resource` nor
    `required_perms` denies every authenticated non-superuser."""
    view = APIView()  # action falls back to "get" -> no verb, no resource
    assert RolePermission().has_permission(_authed_request(), view) is False


def test_fail_closed_unmapped_custom_action():
    """TD-4: a custom @action missing from required_perms and
    DEFAULT_VERB_FOR_ACTION is denied even when `resource` is set."""
    view = APIView()
    view.resource = "students"  # type: ignore[attr-defined]
    view.action = "frobnicate"  # type: ignore[attr-defined]
    assert RolePermission().has_permission(_authed_request("post"), view) is False
