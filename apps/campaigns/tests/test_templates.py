"""F10-2 — reusable message templates: staff name a template + purpose, optionally have
the AI draft its body (low-cost, reusing the apps.ai pipeline), edit it, and reuse it
when composing a campaign (the template's body becomes the campaign message)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

TEMPLATES = "/api/v1/campaigns/templates/"
CAMPAIGNS = "/api/v1/campaigns/"


def _seed_template_ai(tenant):
    from apps.ai.tests.factories import AIPromptFactory, make_budget

    with schema_context(tenant.schema_name):
        AIPromptFactory(
            feature="template_generation",
            version=1,
            system_prompt="Write a message template.",
            user_template="Name: {name}\nPurpose: {purpose}",
            max_output_tokens=512,
            effort="low",
            token_cost_cap=1500,
            is_active=True,
        )
        make_budget(daily_token_limit=1_000_000, monthly_token_limit=10_000_000, is_enabled=True)


def _mock_complete(monkeypatch, text):
    from celery_tasks import ai_tasks

    monkeypatch.setattr(
        ai_tasks, "complete", lambda **kw: {"text": text, "usage": {"input_tokens": 3, "output_tokens": 12}}
    )


def _staff(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    return branch, as_user(tenant, user_in(tenant, roles=[Role.REGISTRAR], branch=branch))


def test_create_template(tenant_a, user_in, as_user):
    _, client = _staff(tenant_a, user_in, as_user)
    r = client.post(
        TEMPLATES,
        {"name": "Lesson reminder", "category": "reminder", "purpose": "remind about class"},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.json()["data"]["name"] == "Lesson reminder"
    assert r.json()["data"]["body"] == ""


def test_ai_generation_fills_the_template_body(tenant_a, user_in, as_user, monkeypatch):
    from celery_tasks import ai_tasks

    _, client = _staff(tenant_a, user_in, as_user)
    _seed_template_ai(tenant_a)
    _mock_complete(monkeypatch, "Dear guardian, your child has a class tomorrow at 10am.")
    tid = client.post(TEMPLATES, {"name": "Reminder", "purpose": "class tomorrow"}, format="json").json()[
        "data"
    ]["id"]

    with schema_context(tenant_a.schema_name):
        from apps.ai.models import AIRequest
        from apps.campaigns.models import MessageTemplate
        from apps.campaigns.services import request_template_generation

        tpl = MessageTemplate.objects.get(pk=tid)
        ai_request = request_template_generation(template=tpl, requested_by=tpl.created_by)
        ai_tasks.run_template_generation(
            ai_request.pk, params={"template_id": tid, "name": tpl.name, "purpose": tpl.purpose}
        )
        ai_request.refresh_from_db()
        assert ai_request.status == AIRequest.Status.SUCCEEDED
        tpl.refresh_from_db()
        assert "class tomorrow" in tpl.body or tpl.body.startswith("Dear guardian")


def test_edit_a_template(tenant_a, user_in, as_user):
    _, client = _staff(tenant_a, user_in, as_user)
    tid = client.post(TEMPLATES, {"name": "t"}, format="json").json()["data"]["id"]
    r = client.patch(f"{TEMPLATES}{tid}/", {"body": "Edited body", "category": "payment"}, format="json")
    assert r.status_code == 200
    assert r.json()["data"]["body"] == "Edited body"
    assert r.json()["data"]["category"] == "payment"


def test_compose_a_campaign_from_a_template(tenant_a, user_in, as_user):
    branch, client = _staff(tenant_a, user_in, as_user)
    tid = client.post(TEMPLATES, {"name": "Reminder"}, format="json").json()["data"]["id"]
    client.patch(f"{TEMPLATES}{tid}/", {"body": "Hello from the template"}, format="json")
    # create a campaign with the template (no explicit message)
    r = client.post(CAMPAIGNS, {"name": "Blast", "template": tid, "branch": branch.id}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["data"]["message"] == "Hello from the template"


def test_campaign_needs_a_message_or_a_template(tenant_a, user_in, as_user):
    branch, client = _staff(tenant_a, user_in, as_user)
    r = client.post(CAMPAIGNS, {"name": "Blast", "branch": branch.id}, format="json")
    assert r.status_code == 400


def test_cannot_supply_both_a_message_and_a_template(tenant_a, user_in, as_user):
    """Exactly one source of text — supplying both is rejected (not silently dropping
    the typed message in favour of the template)."""
    branch, client = _staff(tenant_a, user_in, as_user)
    tid = client.post(TEMPLATES, {"name": "t"}, format="json").json()["data"]["id"]
    client.patch(f"{TEMPLATES}{tid}/", {"body": "template text"}, format="json")
    r = client.post(
        CAMPAIGNS,
        {"name": "Blast", "message": "typed text", "template": tid, "branch": branch.id},
        format="json",
    )
    assert r.status_code == 400


def test_a_template_with_no_body_cannot_be_used(tenant_a, user_in, as_user):
    branch, client = _staff(tenant_a, user_in, as_user)
    tid = client.post(TEMPLATES, {"name": "empty"}, format="json").json()["data"]["id"]  # body empty
    r = client.post(CAMPAIGNS, {"name": "Blast", "template": tid, "branch": branch.id}, format="json")
    assert r.status_code == 400


def test_managing_templates_needs_campaign_write(tenant_a, as_role):
    student, _ = as_role(Role.STUDENT)  # no campaign:write
    assert student.post(TEMPLATES, {"name": "x"}, format="json").status_code == 403
