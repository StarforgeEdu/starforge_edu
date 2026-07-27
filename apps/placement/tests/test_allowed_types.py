"""F8-1 (manager enables types) — a center can restrict which placement question
types its tests use, via CenterSettings.placement_allowed_question_types. Empty =
no restriction (all types). A non-empty list gates BOTH manual authoring (422) and
AI generation (the disallowed types are silently dropped from the batch)."""

from __future__ import annotations

import json

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

SETTINGS = "/api/v1/org/settings/"


def _set_allowed(tenant, types):
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        cs = CenterSettings.load()
        cs.placement_allowed_question_types = types
        cs.save()  # the receiver busts the cached accessor


def _draft(tenant):
    from apps.org.tests.factories import BranchFactory
    from apps.placement import services
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        builder = UserFactory.create()
        return services.create_test(title="t", created_by=builder, branch=branch), branch


def test_no_restriction_allows_every_type(tenant_a):
    from apps.placement import services

    test, _ = _draft(tenant_a)  # default empty list = no restriction
    with schema_context(tenant_a.schema_name):
        q = services.add_question(
            test=test, prompt="even?", question_type="multiple_choice",
            options=["1", "2", "3"], correct_answer=["2"],
        )
        assert q.id  # multiple_choice accepted when no policy is set


def test_manual_authoring_blocks_a_disallowed_type(tenant_a):
    from apps.placement import services
    from core.exceptions import UnprocessableEntity

    _set_allowed(tenant_a, ["single_choice", "writing"])
    test, _ = _draft(tenant_a)
    with schema_context(tenant_a.schema_name):
        # an enabled type still works
        ok = services.add_question(
            test=test, prompt="capital?", question_type="single_choice",
            options=["A", "B"], correct_answer="A",
        )
        assert ok.id
        # a disabled type is refused with a clean 422
        with pytest.raises(UnprocessableEntity) as exc:
            services.add_question(
                test=test, prompt="even?", question_type="multiple_choice",
                options=["1", "2"], correct_answer=["2"],
            )
        assert exc.value.code == "question_type_not_allowed"


def test_ai_generation_drops_disallowed_types(tenant_a):
    """The model may propose any type; only the center-enabled ones are applied."""
    from apps.placement import services
    from apps.placement.models import PlacementQuestion

    _set_allowed(tenant_a, ["single_choice"])
    test, _ = _draft(tenant_a)
    payload = json.dumps(
        [
            {"prompt": "1+1?", "question_type": "single_choice", "options": ["1", "2"], "correct_answer": "2"},
            {"prompt": "evens?", "question_type": "multiple_choice", "options": ["1", "2", "4"],
             "correct_answer": ["2", "4"]},
            {"prompt": "essay", "question_type": "writing"},
        ]
    )
    with schema_context(tenant_a.schema_name):
        added = services.apply_generated_questions(test_id=test.id, output_text=payload)
        assert added == 1  # only the single_choice survived the policy
        types = set(PlacementQuestion.objects.filter(test=test).values_list("question_type", flat=True))
        assert types == {"single_choice"}


def test_setting_round_trips_and_dedupes_through_the_api(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    patched = director.patch(
        SETTINGS,
        {"placement_allowed_question_types": ["writing", "writing", "single_choice"]},
        format="json",
    )
    assert patched.status_code == 200, patched.content
    assert patched.json()["data"]["placement_allowed_question_types"] == ["writing", "single_choice"]  # deduped
    got = director.get(SETTINGS).json()["data"]["placement_allowed_question_types"]
    assert got == ["writing", "single_choice"]


def test_api_rejects_an_unknown_question_type(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    r = director.patch(
        SETTINGS, {"placement_allowed_question_types": ["single_choice", "bogus"]}, format="json"
    )
    assert r.status_code == 400
    body = json.dumps(r.json())
    assert "placement_allowed_question_types" in body or "Unknown question type" in body
