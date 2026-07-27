"""AI-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

import factory

from apps.ai.models import AIFeature, AIPrompt, AIRequest, TenantAIBudget


class AIPromptFactory(factory.django.DjangoModelFactory[AIPrompt]):
    class Meta:
        model = AIPrompt
        django_get_or_create = ("feature", "version")

    feature = AIFeature.ASSIGNMENT_FEEDBACK
    version = 1
    system_prompt = "You are a helpful teacher."
    user_template = "Assignment: {assignment_title}\n{submission_text}\n{student_name}"
    max_output_tokens = 1024
    effort = "medium"
    token_cost_cap = 4000
    is_active = True


class AIRequestFactory(factory.django.DjangoModelFactory[AIRequest]):
    class Meta:
        model = AIRequest

    feature = AIFeature.ASSIGNMENT_FEEDBACK
    status = AIRequest.Status.SUCCEEDED
    prompt = factory.SubFactory(AIPromptFactory)
    source_app = "assignments"
    source_id = factory.Sequence(lambda n: n + 1)
    idempotency_key = factory.Sequence(lambda n: f"assignment_feedback:assignments:{n}:v1")
    input_tokens = 100
    output_tokens = 50


def make_budget(**kwargs) -> TenantAIBudget:
    """Get-or-create the singleton budget (pk=1) with overrides."""
    budget, _ = TenantAIBudget.objects.get_or_create(pk=1)
    if kwargs:
        for field, value in kwargs.items():
            setattr(budget, field, value)
        budget.save()
    return budget
