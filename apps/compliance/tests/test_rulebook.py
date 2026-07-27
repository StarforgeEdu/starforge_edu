"""#12 — rule book: role-filtered rules, forced acknowledgment, version re-accept."""

from __future__ import annotations

import pytest

from core.permissions import Role

pytestmark = pytest.mark.django_db

RULES = "/api/v1/rulebook/rules/"


def _ids(rows):
    return {r["id"] for r in rows}


def test_rule_acknowledge_and_version_reaccept(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    teacher, _ = as_role(Role.TEACHER)

    made = director.post(
        RULES,
        {"title": "No phones in class", "body": "v1 text", "applies_to_roles": ["teacher"]},
        format="json",
    )
    assert made.status_code == 201, made.content
    rid = made.json()["data"]["id"]

    # teacher sees it as pending / not acknowledged
    mine = teacher.get(f"{RULES}mine/").json()["data"]
    assert rid in _ids(mine)
    assert next(r for r in mine if r["id"] == rid)["acknowledged"] is False
    assert rid in _ids(teacher.get(f"{RULES}pending/").json()["data"])

    # teacher accepts -> no longer pending
    assert teacher.post(f"{RULES}{rid}/acknowledge/", {}, format="json").status_code == 200
    assert rid not in _ids(teacher.get(f"{RULES}pending/").json()["data"])

    # director edits the body -> version bumps -> teacher must re-accept
    director.patch(f"{RULES}{rid}/", {"body": "v2 text (updated)"}, format="json")
    assert rid in _ids(teacher.get(f"{RULES}pending/").json()["data"])


def test_rule_role_filter_and_permissions(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    teacher, _ = as_role(Role.TEACHER)
    cashier, _ = as_role(Role.CASHIER)

    rid = director.post(
        RULES, {"title": "Teacher rule", "body": "x", "applies_to_roles": ["teacher"]}, format="json"
    ).json()["data"]["id"]

    # cashier is not targeted -> not in their feed, and can't acknowledge it
    assert rid not in _ids(cashier.get(f"{RULES}mine/").json()["data"])
    assert cashier.post(f"{RULES}{rid}/acknowledge/", {}, format="json").status_code == 403

    # a teacher can't author rules (no compliance:write)
    assert teacher.post(RULES, {"title": "x", "body": "y"}, format="json").status_code == 403


def test_read_only_token_cannot_acknowledge(tenant_a, as_role, client_for):
    """A read-only impersonation session may not forge a rule acknowledgment (acknowledge
    is a write with no perm code, so it must reinstate the read-only-token deny)."""
    from core.session_auth import create_session

    director, _ = as_role(Role.DIRECTOR)
    _tc, teacher_user = as_role(Role.TEACHER)
    rid = director.post(
        RULES, {"title": "R", "body": "b", "applies_to_roles": ["teacher"]}, format="json"
    ).json()["data"]["id"]

    from django_tenants.utils import schema_context

    with schema_context(tenant_a.schema_name):
        ro_key = create_session(teacher_user, read_only=True).key
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {ro_key}")
    resp = client.post(f"{RULES}{rid}/acknowledge/", {}, format="json")
    assert resp.status_code == 403
    assert resp.json()["code"] == "read_only_token"
