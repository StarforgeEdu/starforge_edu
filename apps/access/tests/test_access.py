"""A-2 — dynamic, center-configurable permissions.

A center grants/revokes permission codes per role and the change is enforced on
the very next request (overrides are read per-request, no staleness window). The
static matrix is the default; the master wildcard and the `access` resource are
not overridable, so the director's authority and control of permission management
are immutable through this mechanism."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.access.services import set_override
from core.permissions import Role, roles_with_permission

pytestmark = pytest.mark.django_db

OVERRIDES = "/api/v1/access/overrides/"


def test_grant_override_grants_access_live(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    assert teacher.get("/api/v1/finance/invoices/").status_code == 403  # no finance by default
    with schema_context(tenant_a.schema_name):
        set_override(role=Role.TEACHER, permission="finance:read", effect="grant")
    assert teacher.get("/api/v1/finance/invoices/").status_code == 200


def test_grant_resource_wildcard_broadens(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    with schema_context(tenant_a.schema_name):
        set_override(role=Role.TEACHER, permission="finance:*", effect="grant")
    # finance:* covers finance:read on the invoices list.
    assert teacher.get("/api/v1/finance/invoices/").status_code == 200


def test_revoke_literal_permission_removes_access(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    assert teacher.get("/api/v1/students/").status_code == 200  # holds students:read literally
    with schema_context(tenant_a.schema_name):
        set_override(role=Role.TEACHER, permission="students:read", effect="revoke")
    assert teacher.get("/api/v1/students/").status_code == 403


def test_revoke_carves_a_verb_out_of_a_resource_wildcard(tenant_a, as_role):
    """The dangerous case: a role holds students:* (head_of_dept). Revoking
    students:read must actually deny it, not be silently masked by the wildcard."""
    hod, _ = as_role(Role.HEAD_OF_DEPT)
    assert hod.get("/api/v1/students/").status_code == 200  # via students:*
    with schema_context(tenant_a.schema_name):
        set_override(role=Role.HEAD_OF_DEPT, permission="students:read", effect="revoke")
    assert hod.get("/api/v1/students/").status_code == 403  # carve-out enforced


def test_director_wildcard_is_not_overridable(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    resp = director.post(
        OVERRIDES, {"role": "teacher", "permission": "*:*", "effect": "revoke"}, format="json"
    )
    assert resp.status_code == 400

    # Even a stored revoke of a specific code can't lock out the *:* holder.
    with schema_context(tenant_a.schema_name):
        set_override(role=Role.DIRECTOR, permission="students:read", effect="revoke")
    assert director.get("/api/v1/students/").status_code == 200


def test_access_resource_is_not_overridable(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    resp = director.post(
        OVERRIDES, {"role": "teacher", "permission": "access:write", "effect": "grant"}, format="json"
    )
    assert resp.status_code == 400  # permission management is not delegable


def test_superuser_is_immune_to_revoke(tenant_a, user_in, as_user):
    su = as_user(tenant_a, user_in(tenant_a, is_superuser=True))
    with schema_context(tenant_a.schema_name):
        # Revoke from every role; the superuser bypasses role checks entirely.
        set_override(role=Role.TEACHER, permission="students:read", effect="revoke")
    assert su.get("/api/v1/students/").status_code == 200


def test_effective_roles_endpoint_reflects_overrides(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        set_override(role=Role.TEACHER, permission="finance:read", effect="grant")
        set_override(role=Role.HEAD_OF_DEPT, permission="students:read", effect="revoke")

    body = director.get("/api/v1/access/roles/").json()["data"]
    assert "finance:read" in body["roles"]["teacher"]["granted"]
    assert "students:read" in body["roles"]["head_of_dept"]["revoked"]
    assert "*:*" in body["roles"]["director"]["granted"]


def test_only_privileged_role_can_manage_overrides(tenant_a, as_role):
    payload = {"role": "librarian", "permission": "finance:read", "effect": "grant"}
    teacher, _ = as_role(Role.TEACHER)
    assert teacher.post(OVERRIDES, payload, format="json").status_code == 403

    director, _ = as_role(Role.DIRECTOR)
    resp = director.post(OVERRIDES, payload, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["data"]["created_by"] is not None  # stamped


def test_non_privileged_role_cannot_read_access_views(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    assert teacher.get("/api/v1/access/roles/").status_code == 403
    assert teacher.get("/api/v1/access/permissions/").status_code == 403


def test_duplicate_override_rejected(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    payload = {"role": "teacher", "permission": "finance:read", "effect": "grant"}
    assert director.post(OVERRIDES, payload, format="json").status_code == 201
    assert director.post(OVERRIDES, payload, format="json").status_code == 400


def test_permission_catalog(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    body = director.get("/api/v1/access/permissions/").json()["data"]
    assert "students:read" in body["permissions"]
    assert "approvals:disburse" in body["permissions"]
    assert "notifications:write" in body["permissions"]  # implicit under director *:*
    assert "*:*" not in body["permissions"]


@pytest.mark.parametrize("permission", ["finance:Read", "student:read", "unknown:*", "finance:unknown"])
def test_http_rejects_unknown_permission_codes(tenant_a, as_role, permission):
    director, _ = as_role(Role.DIRECTOR)
    response = director.post(
        OVERRIDES,
        {"role": Role.TEACHER, "permission": permission, "effect": "grant"},
        format="json",
    )
    assert response.status_code == 400
    assert "permission" in response.json()["errors"]


def test_service_rejects_master_wildcard_and_access(tenant_a):
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        with pytest.raises(ValidationException):
            set_override(role=Role.TEACHER, permission="*:*", effect="grant")
        with pytest.raises(ValidationException):
            set_override(role=Role.TEACHER, permission="access:write", effect="grant")


@pytest.mark.parametrize(
    ("role", "permission", "effect"),
    [
        ("Teacher", "finance:read", "grant"),
        (Role.TEACHER, "finance:Read", "grant"),
        (Role.TEACHER, "finance:read", "allow"),
    ],
)
def test_programmatic_service_rejects_invalid_override_values(tenant_a, role, permission, effect):
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name), pytest.raises(ValidationException):
        set_override(role=role, permission=permission, effect=effect)


def test_overrides_flow_into_recipient_routing(tenant_a):
    # roles_with_permission (used to find who can disburse/approve, etc.) must also
    # honor overrides — the dynamic grant reaches anti-fraud notification routing.
    with schema_context(tenant_a.schema_name):
        assert Role.LIBRARIAN not in roles_with_permission("approvals:disburse")
        set_override(role=Role.LIBRARIAN, permission="approvals:disburse", effect="grant")
        assert Role.LIBRARIAN in roles_with_permission("approvals:disburse")
