"""F3-4 — AI analysis of form responses: a manager asks the AI to summarize a form's
results (narrative + key takeaways); the output is stored on the AIRequest, charts
come from /summary/. Respondent names are redacted before the model sees them.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

FORMS = "/api/v1/forms/"


def _seed_form_ai(tenant):
    from apps.ai.tests.factories import AIPromptFactory, make_budget

    with schema_context(tenant.schema_name):
        AIPromptFactory(
            feature="form_analysis",
            version=1,
            system_prompt="Analyze the form results.",
            user_template="Form: {form_title}\n\n{aggregate}\n\nComments:\n{comments}",
            max_output_tokens=1024,
            effort="medium",
            token_cost_cap=4000,
            is_active=True,
        )
        make_budget(daily_token_limit=1_000_000, monthly_token_limit=10_000_000, is_enabled=True)


def _published_form(director):
    fid = director.post(FORMS, {"title": "Feedback"}, format="json").json()["data"]["id"]
    field = director.post(
        f"{FORMS}{fid}/fields/", {"label": "Comments", "field_type": "textarea"}, format="json"
    ).json()["data"]["id"]
    pub = director.post(f"{FORMS}{fid}/publish/", {}, format="json")
    assert pub.status_code == 200, pub.content
    return fid, field


def _submit(student, fid, field, value):
    r = student.post(f"{FORMS}{fid}/submit/", {"answers": [{"field": field, "value": value}]}, format="json")
    assert r.status_code == 201, r.content


def _mock_complete(monkeypatch, capture=None):
    from celery_tasks import ai_tasks

    def _fake(*, system, messages, max_tokens, effort):
        if capture is not None:
            capture["text"] = messages[0]["content"]
        return {
            "text": "Overall positive. Key takeaways: ...",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }

    monkeypatch.setattr(ai_tasks, "complete", _fake)


def test_analyze_task_stores_a_narrative(tenant_a, as_role, monkeypatch):
    from celery_tasks import ai_tasks

    _seed_form_ai(tenant_a)
    _mock_complete(monkeypatch)
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, field = _published_form(director)
    _submit(student, fid, field, "Great class, learned a lot.")
    with schema_context(tenant_a.schema_name):
        from apps.ai.models import AIRequest
        from apps.forms.models import Form
        from apps.forms.services import request_form_analysis

        form = Form.objects.get(pk=fid)
        ai_request = request_form_analysis(form=form, requested_by=None)
        ai_tasks.run_form_analysis(ai_request.pk, params={"form_id": form.id})
        ai_request.refresh_from_db()
        assert ai_request.status == AIRequest.Status.SUCCEEDED
        assert ai_request.output_text


def test_analyze_redacts_respondent_name_before_sending(tenant_a, as_role, monkeypatch):
    """A non-anonymous form's respondent name appearing in a free-text answer is
    tokenized before the prompt reaches the model (PII never leaves the server)."""
    _seed_form_ai(tenant_a)
    captured: dict = {}
    _mock_complete(monkeypatch, capture=captured)
    director, _ = as_role(Role.DIRECTOR)
    student, student_user = as_role(Role.STUDENT)
    with schema_context(tenant_a.schema_name):
        student_user.first_name = "Ali"
        student_user.last_name = "Valiyev"
        student_user.save()
    fid, field = _published_form(director)
    _submit(student, fid, field, "I, Ali Valiyev, thought it was great.")
    with schema_context(tenant_a.schema_name):
        from apps.forms.models import Form
        from apps.forms.services import request_form_analysis
        from celery_tasks import ai_tasks

        form = Form.objects.get(pk=fid)
        ai_request = request_form_analysis(form=form)
        ai_tasks.run_form_analysis(ai_request.pk, params={"form_id": form.id})
    assert "Valiyev" not in captured["text"]
    assert "Ali Valiyev" not in captured["text"]


def test_analyze_bounds_huge_comment_volume(tenant_a, as_role, monkeypatch):
    """A large form can't push the prompt past the reserved token budget — the
    free-text volume sent to the model is capped."""
    _seed_form_ai(tenant_a)
    captured: dict = {}
    _mock_complete(monkeypatch, capture=captured)
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, field = _published_form(director)
    _submit(student, fid, field, "x" * 50_000)  # a huge single answer
    with schema_context(tenant_a.schema_name):
        from apps.forms.models import Form
        from apps.forms.services import request_form_analysis
        from celery_tasks import ai_tasks

        form = Form.objects.get(pk=fid)
        ai_request = request_form_analysis(form=form)
        ai_tasks.run_form_analysis(ai_request.pk, params={"form_id": form.id})
    assert len(captured["text"]) < 20_000  # bounded, not the raw 50k


def test_analyze_endpoint_returns_202(tenant_a, as_role, monkeypatch):
    _seed_form_ai(tenant_a)
    _mock_complete(monkeypatch)
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, field = _published_form(director)
    _submit(student, fid, field, "ok")
    r = director.post(f"{FORMS}{fid}/analyze/", {}, format="json")
    assert r.status_code == 202, r.content
    assert r.json()["data"]["request_id"]


def test_analyze_rejects_a_form_with_no_responses(tenant_a, as_role):
    _seed_form_ai(tenant_a)
    director, _ = as_role(Role.DIRECTOR)
    fid, _field = _published_form(director)
    r = director.post(f"{FORMS}{fid}/analyze/", {}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "no_responses"


def test_responder_cannot_analyze(tenant_a, as_role, monkeypatch):
    _seed_form_ai(tenant_a)
    _mock_complete(monkeypatch)
    director, _ = as_role(Role.DIRECTOR)
    student, _ = as_role(Role.STUDENT)
    fid, field = _published_form(director)
    _submit(student, fid, field, "ok")
    # a respondent holds forms:read (submit) but not forms:write (analyze)
    assert student.post(f"{FORMS}{fid}/analyze/", {}, format="json").status_code in (403, 404)
