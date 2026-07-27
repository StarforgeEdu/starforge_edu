"""F8-2: placement test AUTHORING can be restricted to the mobile app via the
CenterSettings.placement_test_creation_mobile_only flag. When on, a request that does not
identify as the mobile client (`X-Client: mobile`) is 403'd; a soft, spoofable steering
gate. Off by default — web authoring works as before."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TESTS = "/api/v1/placement/tests/"
SETTINGS = "/api/v1/org/settings/"


def _set_mobile_only(tenant, value):
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        cs = CenterSettings.load()
        cs.placement_test_creation_mobile_only = value
        cs.save()  # the receiver busts the cached settings accessor the gate reads


def _teacher(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    return as_user(tenant, user_in(tenant, roles=[Role.TEACHER], branch=branch)), branch


def test_web_authoring_allowed_when_flag_off(tenant_a, user_in, as_user):
    client, branch = _teacher(tenant_a, user_in, as_user)
    r = client.post(TESTS, {"title": "T", "branch": branch.id}, format="json")  # no X-Client header
    assert r.status_code == 201, r.content


def test_web_authoring_blocked_when_flag_on(tenant_a, user_in, as_user):
    _set_mobile_only(tenant_a, True)
    client, branch = _teacher(tenant_a, user_in, as_user)
    r = client.post(TESTS, {"title": "T", "branch": branch.id}, format="json")  # no X-Client header
    assert r.status_code == 403
    assert r.json()["code"] == "web_test_creation_blocked"


def test_mobile_authoring_allowed_when_flag_on(tenant_a, user_in, as_user):
    _set_mobile_only(tenant_a, True)
    client, branch = _teacher(tenant_a, user_in, as_user)
    r = client.post(
        TESTS, {"title": "T", "branch": branch.id}, format="json", HTTP_X_CLIENT="mobile"
    )
    assert r.status_code == 201, r.content


def test_gate_covers_the_whole_authoring_flow(tenant_a, user_in, as_user):
    """Not just create — add-question is gated too (the full authoring surface)."""
    client, branch = _teacher(tenant_a, user_in, as_user)
    tid = client.post(TESTS, {"title": "T", "branch": branch.id}, format="json").json()["data"]["id"]
    _set_mobile_only(tenant_a, True)
    q = {"prompt": "2+2?", "question_type": "single_choice", "options": ["3", "4"], "correct_answer": "4"}
    assert client.post(f"{TESTS}{tid}/questions/", q, format="json").status_code == 403  # web
    assert (
        client.post(f"{TESTS}{tid}/questions/", q, format="json", HTTP_X_CLIENT="mobile").status_code
        == 201  # mobile
    )


def test_flag_settable_via_settings_api(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    r = director.patch(SETTINGS, {"placement_test_creation_mobile_only": True}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["data"]["placement_test_creation_mobile_only"] is True
    assert director.get(SETTINGS).json()["data"]["placement_test_creation_mobile_only"] is True
