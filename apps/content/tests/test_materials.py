"""F9-1 — AI-drafted library materials: a manager creates a DRAFT (title + topic), has
the AI draft its body (reusing the apps.ai pipeline), reviews/edits it, then PUBLISHES
it so learners with library access can read it. A draft never leaks to learners."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

MATERIALS = "/api/v1/content/materials/"


def _seed_material_ai(tenant):
    from apps.ai.tests.factories import AIPromptFactory, make_budget

    with schema_context(tenant.schema_name):
        AIPromptFactory(
            feature="material_generation",
            version=1,
            system_prompt="Write teaching material.",
            user_template="Title: {title}\nTopic: {topic}",
            max_output_tokens=2048,
            effort="medium",
            token_cost_cap=6000,
            is_active=True,
        )
        make_budget(daily_token_limit=1_000_000, monthly_token_limit=10_000_000, is_enabled=True)


def _mock_complete(monkeypatch, text):
    from celery_tasks import ai_tasks

    monkeypatch.setattr(
        ai_tasks, "complete", lambda **kw: {"text": text, "usage": {"input_tokens": 5, "output_tokens": 50}}
    )


def _library(tenant, **over):
    from apps.content.tests.factories import ContentLibraryFactory

    with schema_context(tenant.schema_name):
        return ContentLibraryFactory.create(**over)


def _setup(tenant, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
    teacher_u = user_in(tenant, roles=[Role.TEACHER], branch=branch)  # content:read/write/publish
    student_u = user_in(tenant, roles=[Role.STUDENT], branch=branch)  # content:read only
    return {
        "branch": branch,
        "library": _library(tenant),
        "teacher_u": teacher_u,
        "manager": as_user(tenant, teacher_u),
        "learner": as_user(tenant, student_u),
    }


def _create_draft(s, **over):
    payload = {"library": s["library"].id, "title": "Photosynthesis", "topic": "how plants make food"}
    payload.update(over)
    return s["manager"].post(MATERIALS, payload, format="json")


def test_create_draft_material(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = _create_draft(s)
    assert r.status_code == 201, r.content
    body = r.json()["data"]
    assert body["status"] == "draft"
    assert body["body"] == ""
    assert body["title"] == "Photosynthesis"


def test_ai_generation_fills_the_body(tenant_a, user_in, as_user, monkeypatch):
    from celery_tasks import ai_tasks

    s = _setup(tenant_a, user_in, as_user)
    _seed_material_ai(tenant_a)
    _mock_complete(monkeypatch, "# Photosynthesis\n\nPlants convert light into energy...")
    mid = _create_draft(s).json()["data"]["id"]

    with schema_context(tenant_a.schema_name):
        from apps.ai.models import AIRequest
        from apps.content.models import LibraryMaterial
        from apps.content.services import request_material_generation

        material = LibraryMaterial.objects.get(pk=mid)
        ai_request = request_material_generation(material=material, requested_by=s["teacher_u"])
        ai_tasks.run_material_generation(
            ai_request.pk,
            params={"material_id": mid, "title": material.title, "topic": material.topic},
        )
        ai_request.refresh_from_db()
        assert ai_request.status == AIRequest.Status.SUCCEEDED
        material.refresh_from_db()
        assert material.body.startswith("# Photosynthesis")
        assert material.status == "draft"  # still a draft — a human publishes


def test_generation_does_not_touch_a_published_material(tenant_a, user_in, as_user):
    """apply is non-destructive: a published material's body is never overwritten."""
    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        from apps.content.models import LibraryMaterial
        from apps.content.services import apply_generated_material

        m = LibraryMaterial.objects.create(
            library=s["library"], title="x", body="final", status=LibraryMaterial.Status.PUBLISHED
        )
        applied = apply_generated_material(material_id=m.id, output_text="SHOULD NOT APPEAR")
        assert applied is False
        m.refresh_from_db()
        assert m.body == "final"


def test_edit_then_publish(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    mid = _create_draft(s).json()["data"]["id"]
    # hand-edit the draft body
    patched = s["manager"].patch(f"{MATERIALS}{mid}/", {"body": "Hand-written body."}, format="json")
    assert patched.status_code == 200
    assert patched.json()["data"]["body"] == "Hand-written body."
    # publish it
    pub = s["manager"].post(f"{MATERIALS}{mid}/publish/", {}, format="json")
    assert pub.status_code == 200
    assert pub.json()["data"]["status"] == "published"
    assert pub.json()["data"]["published_at"]


def test_a_publish_only_manager_can_publish_a_draft(tenant_a, user_in, as_user):
    """The designated publisher (content:publish) — an HOD who does NOT hold content:write
    — must still see + publish a draft (the maker-checker: the author isn't the publisher)."""
    s = _setup(tenant_a, user_in, as_user)
    hod = as_user(tenant_a, user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=s["branch"]))
    mid = _create_draft(s).json()["data"]["id"]  # authored + body-filled by the teacher
    s["manager"].patch(f"{MATERIALS}{mid}/", {"body": "ready to publish"}, format="json")
    # the HOD can retrieve the draft to review it, then publish it
    assert hod.get(f"{MATERIALS}{mid}/").status_code == 200
    pub = hod.post(f"{MATERIALS}{mid}/publish/", {}, format="json")
    assert pub.status_code == 200, pub.content
    assert pub.json()["data"]["status"] == "published"


def test_cannot_publish_an_empty_material(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    mid = _create_draft(s).json()["data"]["id"]  # body empty
    r = s["manager"].post(f"{MATERIALS}{mid}/publish/", {}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "material_empty"


def test_cannot_edit_a_published_material(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    mid = _create_draft(s, title="t").json()["data"]["id"]
    s["manager"].patch(f"{MATERIALS}{mid}/", {"body": "b"}, format="json")
    s["manager"].post(f"{MATERIALS}{mid}/publish/", {}, format="json")
    r = s["manager"].patch(f"{MATERIALS}{mid}/", {"body": "changed"}, format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "material_not_draft"


def test_learner_sees_only_published_materials(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    # one draft, one published
    draft_id = _create_draft(s, title="draft one").json()["data"]["id"]
    pub_id = _create_draft(s, title="pub one").json()["data"]["id"]
    s["manager"].patch(f"{MATERIALS}{pub_id}/", {"body": "ready"}, format="json")
    s["manager"].post(f"{MATERIALS}{pub_id}/publish/", {}, format="json")

    ids = [m["id"] for m in s["learner"].get(MATERIALS).json()["data"]]
    assert pub_id in ids  # the published one is visible
    assert draft_id not in ids  # the draft is NOT
    # and a learner cannot create
    assert (
        s["learner"].post(MATERIALS, {"library": s["library"].id, "title": "x"}, format="json").status_code
        == 403
    )


def test_cannot_add_material_to_an_inaccessible_library(tenant_a, user_in, as_user):
    """A writer can only add materials to a library they can access (scoped writes)."""
    s = _setup(tenant_a, user_in, as_user)
    from apps.content.models import ContentLibrary

    hidden = _library(tenant_a, visibility=ContentLibrary.Visibility.ROLE, allowed_roles=["director"])
    r = _create_draft(s, library=hidden.id)
    assert r.status_code == 403
    assert r.json()["code"] == "library_out_of_scope"
