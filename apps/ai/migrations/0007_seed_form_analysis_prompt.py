"""Seed the active AIPrompt for form-response analysis (F3-4).

The model receives the aggregate tallies + redacted free-text answers and writes a
short narrative + key takeaways stored on the ``AIRequest`` output (the manager's
analysis view; charts come from ``forms.form_summary``). ``user_template`` uses
``str.format`` placeholders the task fills in. Idempotent via ``update_or_create``.
"""

from __future__ import annotations

from django.db import migrations

PROMPT = {
    "feature": "form_analysis",
    "version": 1,
    "system_prompt": (
        "You analyze survey/form results for an education centre manager. Given the aggregate "
        "tallies and the free-text comments, write a concise analysis: 2-4 sentences of overall "
        "narrative, then 3-6 bullet 'key takeaways' (themes, standouts, and any action to consider). "
        "Be faithful to the data; never invent numbers or names. Keep it under 250 words."
    ),
    "user_template": (
        "Form: {form_title}\n\n"
        "Aggregate results:\n{aggregate}\n\n"
        "Free-text comments:\n{comments}\n\n"
        "Write the analysis."
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
        ("ai_app", "0006_alter_aiprompt_feature_alter_airequest_feature"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
