"""Per-Center configurable enrollment reasons (the old ReasonCode enum → the
EnrollmentReason table): defaults seeded per tenant, manager CRUD, and the
transition endpoint validates reason_code against the active reasons."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.students.services import create_student
from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/students/enrollment-reasons/"


def _registrar(tenant, user_in, as_user):
    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    return branch, as_user(tenant, user_in(tenant, roles=[Role.REGISTRAR], branch=branch))


def test_defaults_seeded_per_tenant(tenant_a, user_in, as_user):
    _, client = _registrar(tenant_a, user_in, as_user)
    slugs = {r["slug"] for r in client.get(URL).json()["data"]}
    assert {"completed", "financial", "other"} <= slugs  # migration seeded the 6 defaults


def test_manager_creates_reason_autoslug_and_dup_rejected(tenant_a, user_in, as_user):
    _, client = _registrar(tenant_a, user_in, as_user)
    created = client.post(URL, {"name": "Moved abroad", "color": "#ef4444"}, format="json")
    assert created.status_code == 201, created.content
    assert created.json()["data"]["slug"] == "moved-abroad"  # auto-derived from the name
    dup = client.post(URL, {"name": "Moved abroad"}, format="json")
    assert dup.status_code == 400  # slug collision


def test_teacher_can_list_but_not_create_reason(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    teacher = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    assert teacher.get(URL).status_code == 200  # students:read may list
    assert teacher.post(URL, {"name": "X"}, format="json").status_code == 403  # no students:write


def test_transition_accepts_configured_reason_and_rejects_unknown(tenant_a, user_in, as_user):
    branch, client = _registrar(tenant_a, user_in, as_user)
    made = client.post(URL, {"name": "Moved abroad", "slug": "moved_abroad"}, format="json")
    assert made.status_code == 201, made.content
    with schema_context(tenant_a.schema_name):
        s1 = create_student(branch=branch, phone="+998905558020").id  # default LEAD (no seat cost)
        s2 = create_student(branch=branch, phone="+998905558021").id

    ok = client.post(
        f"/api/v1/students/{s1}/transition/",
        {"to_status": "application", "reason_code": "moved_abroad"},  # center-defined reason accepted
        format="json",
    )
    assert ok.status_code == 200, ok.content

    bad = client.post(
        f"/api/v1/students/{s2}/transition/",
        {"to_status": "application", "reason_code": "not_a_real_reason"},  # unknown -> 400
        format="json",
    )
    assert bad.status_code == 400


def test_long_reason_slug_can_be_recorded(tenant_a, user_in, as_user):
    """Regression: a valid active reason whose slug is >32 chars (up to the 64-char
    SlugField limit) must actually be recordable by a transition — the denormalized
    reason_code column has to match the slug width, not the old varchar(32)."""
    branch, client = _registrar(tenant_a, user_in, as_user)
    long_slug = "relocated-to-another-country-for-family-reasons"  # 47 chars
    made = client.post(URL, {"name": "Relocated abroad", "slug": long_slug}, format="json")
    assert made.status_code == 201, made.content
    with schema_context(tenant_a.schema_name):
        sid = create_student(branch=branch, phone="+998905558030").id
    resp = client.post(
        f"/api/v1/students/{sid}/transition/",
        {"to_status": "application", "reason_code": long_slug},
        format="json",
    )
    assert resp.status_code == 200, resp.content  # was DataError->400 before the width fix
