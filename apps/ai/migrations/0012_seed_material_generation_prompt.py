"""Seed the active AIPrompt for library-material generation (F9-1).

The model receives a title + the author's topic brief and drafts clear teaching
material (markdown body), which ``apps.content.services.apply_generated_material``
writes onto the DRAFT material for a manager to review + publish. ``user_template``
uses ``str.format`` placeholders the task fills in. Idempotent via ``update_or_create``.
"""

from __future__ import annotations

from django.db import migrations

PROMPT = {
    "feature": "material_generation",
    "version": 1,
    "system_prompt": (
        "You write clear, accurate teaching material for an education centre. Produce well-"
        "structured content in Markdown (headings, short paragraphs, examples, and a brief summary) "
        "suitable for a teacher to hand to learners. Be factual and age-appropriate. Output ONLY the "
        "material body — no preamble, no meta commentary, no code fences around the whole document."
    ),
    "user_template": (
        "Write a teaching material titled \"{title}\".\n\nIt should cover: {topic}\n\n"
        "Return the material body in Markdown."
    ),
    "max_output_tokens": 2048,
    "effort": "medium",
    "token_cost_cap": 6000,
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
        ("ai_app", "0011_alter_aiprompt_feature_alter_airequest_feature"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
