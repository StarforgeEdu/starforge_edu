"""Seed the active AIPrompt for placement writing marking (F8-3).

The model receives each writing question + the lead's response + the max points,
and returns a JSON array of {question_id, score} that
``apps.placement.services.apply_writing_marks`` clamps + applies to the WRITING
answers, then recomputes the attempt grade. ``user_template`` uses ``str.format``
placeholders the task fills in. Idempotent via ``update_or_create``.
"""

from __future__ import annotations

from django.db import migrations

PROMPT = {
    "feature": "writing_marking",
    "version": 1,
    "system_prompt": (
        "You mark placement-test writing answers for an education centre. For EACH answer, award an "
        "integer score from 0 to its max points based on grammar, coherence, and how well it addresses "
        "the prompt. Output ONLY a JSON array of objects {\"question_id\": int, \"score\": int} — no "
        "prose, no markdown fences. Never exceed an answer's max points. Be fair and consistent."
    ),
    "user_template": (
        "Mark these writing answers (each shows its question id and max points):\n\n{items}\n\n"
        "Return the JSON array of scores."
    ),
    "max_output_tokens": 1024,
    "effort": "medium",
    "token_cost_cap": 4000,
    "is_active": True,
}


def seed(apps, schema_editor):
    AIPrompt = apps.get_model("ai_app", "AIPrompt")
    AIPrompt.objects.update_or_create(
        feature=PROMPT["feature"],
        version=PROMPT["version"],
        defaults={k: v for k, v in PROMPT.items() if k not in ("feature", "version")},
    )


def unseed(apps, schema_editor):
    AIPrompt = apps.get_model("ai_app", "AIPrompt")
    AIPrompt.objects.filter(feature=PROMPT["feature"], version=PROMPT["version"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ai_app", "0008_alter_aiprompt_feature_alter_airequest_feature"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
