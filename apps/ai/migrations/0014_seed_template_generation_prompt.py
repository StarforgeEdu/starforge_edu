"""Seed the active AIPrompt for message-template generation (F10-2).

The model receives a template name + the author's purpose brief and drafts a short,
reusable message body, which ``apps.campaigns.services.apply_generated_template`` writes
onto the template. Low-cost (short output / low effort). ``user_template`` uses
``str.format`` placeholders the task fills in. Idempotent via ``update_or_create``.
"""

from __future__ import annotations

from django.db import migrations

PROMPT = {
    "feature": "template_generation",
    "version": 1,
    "system_prompt": (
        "You write short, friendly, reusable message templates for an education centre to send to "
        "students and their guardians (e.g. SMS reminders, announcements, payment nudges). Keep it "
        "concise and clear, no more than a few sentences, polite and culturally neutral. Output ONLY "
        "the message text — no preamble, no quotes, no markdown."
    ),
    "user_template": "Write a message template named \"{name}\". Purpose: {purpose}",
    "max_output_tokens": 512,
    "effort": "low",
    "token_cost_cap": 1500,
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
        ("ai_app", "0013_alter_aiprompt_feature_alter_airequest_feature"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
