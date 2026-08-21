"""Seed one active AIPrompt per feature (D4-LA-1).

Each tenant schema gets a v1 active prompt for assignment_feedback,
exam_generation, and content_summary. ``user_template`` uses ``str.format``
placeholders the tasks fill in (celery_tasks/ai_tasks.py). The reverse drops the
seeded rows. Idempotent via ``update_or_create`` so re-running is harmless.
"""

from __future__ import annotations

from django.db import migrations

PROMPTS = [
    {
        "feature": "assignment_feedback",
        "version": 1,
        "system_prompt": (
            "You are an experienced teacher giving constructive, encouraging feedback on a "
            "student's submission. Be specific, cite strengths first, then concrete areas to "
            "improve. Never reveal or invent personal data. Keep it under 200 words."
        ),
        "user_template": (
            "Assignment: {assignment_title}\n\n"
            "Student ({student_name}) submission:\n{submission_text}\n\n"
            "Write feedback for this submission."
        ),
        "max_output_tokens": 1024,
        "effort": "medium",
        "token_cost_cap": 4000,
        "is_active": True,
    },
    {
        "feature": "exam_generation",
        "version": 1,
        "system_prompt": (
            "You are an expert exam author. Produce clear, unambiguous questions with an answer "
            "key. Match the requested difficulty and count exactly. Output well-structured text."
        ),
        "user_template": (
            "Subject: {subject_name}\n"
            "Exam type: {exam_type}\n"
            "Number of questions: {question_count}\n"
            "Difficulty: {difficulty}\n\n"
            "Generate the exam."
        ),
        "max_output_tokens": 4096,
        "effort": "high",
        "token_cost_cap": 12000,
        "is_active": True,
    },
    {
        "feature": "content_summary",
        "version": 1,
        "system_prompt": (
            "You summarize lesson materials into a concise study aid: 3-6 bullet points capturing "
            "the key concepts a student should remember. Be faithful to the source."
        ),
        "user_template": (
            "File title: {file_title}\nFile type: {file_type}\n\n"
            "Summarize this lesson material into key study points."
        ),
        "max_output_tokens": 1024,
        "effort": "medium",
        "token_cost_cap": 3000,
        "is_active": True,
    },
]


def seed_prompts(apps, schema_editor):
    AIPrompt = apps.get_model("ai_app", "AIPrompt")
    for spec in PROMPTS:
        AIPrompt.objects.update_or_create(
            feature=spec["feature"],
            version=spec["version"],
            defaults={k: v for k, v in spec.items() if k not in ("feature", "version")},
        )


def unseed_prompts(apps, schema_editor):
    AIPrompt = apps.get_model("ai_app", "AIPrompt")
    AIPrompt.objects.filter(version=1, feature__in=[p["feature"] for p in PROMPTS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ai_app", "0002_aiprompt_airequest_tenantaibudget_delete_aiitem_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_prompts, unseed_prompts),
    ]
