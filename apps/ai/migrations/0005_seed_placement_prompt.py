"""Seed the active AIPrompt for placement-test generation (F1-3).

The model emits a JSON array of question objects that
``apps.placement.services.apply_generated_questions`` parses + validates into
DRAFT PlacementQuestions. ``user_template`` uses ``str.format`` placeholders the
task fills in (celery_tasks/ai_tasks.py:run_placement_generation). Idempotent via
``update_or_create``; the reverse drops the seeded row.
"""

from __future__ import annotations

from django.db import migrations

PROMPT = {
    "feature": "placement_generation",
    "version": 1,
    "system_prompt": (
        "You are an expert placement-test author for a language/exam-prep centre. Output ONLY a "
        "JSON array of question objects — no prose, no markdown fences. Each object is "
        '{"prompt": str, "question_type": "single_choice"|"true_false"|"writing", '
        '"options": [str, ...] (>=2 unique, ONLY for single_choice), '
        '"correct_answer": one of the options for single_choice / true or false for true_false / '
        'omit for writing, "points": int >= 1}. Produce exactly the requested number of questions '
        "at the requested difficulty. Make questions clear and unambiguous."
    ),
    "user_template": (
        "Subject: {subject}\n"
        "Number of questions: {count}\n"
        "Difficulty: {difficulty}\n"
        "Topic focus: {topic}\n\n"
        "Generate the placement questions as a JSON array."
    ),
    "max_output_tokens": 4096,
    "effort": "high",
    "token_cost_cap": 12000,
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
        ("ai_app", "0004_airequest_reserved_tokens"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
