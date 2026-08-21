"""D4-LB-1: seed the six library Report rows per tenant.

Idempotent via update_or_create on the unique ``key`` — re-running never
duplicates and refreshes title/roles. ``allowed_roles`` is the per-report
visibility list (also enforced row-wise in selectors for teacher cohort scoping).
Role codes are the literals from ``core.permissions.Role``.
"""

from django.db import migrations

# Director (*:*) sees everything regardless; listing director here is explicit so
# the library/allowed_roles surface is self-describing.
_DIRECTOR = "director"
_HEAD = "head_of_dept"
_TEACHER = "teacher"
_ACCOUNTANT = "accountant"

REPORTS = [
    {
        "key": "enrollment",
        "title": "Enrollment report",
        "description": "Active/enrolled students by branch and cohort.",
        "allowed_roles": [_DIRECTOR, _HEAD, _TEACHER],
        "default_format": "pdf",
    },
    {
        "key": "attendance",
        "title": "Attendance report",
        "description": "Attendance records and per-status counts over a date range.",
        "allowed_roles": [_DIRECTOR, _HEAD, _TEACHER],
        "default_format": "pdf",
    },
    {
        "key": "grades",
        "title": "Grades report",
        "description": "Published grades per student/subject/term.",
        "allowed_roles": [_DIRECTOR, _HEAD, _TEACHER],
        "default_format": "pdf",
    },
    {
        "key": "finance",
        "title": "Finance report",
        "description": "Invoice totals, paid/outstanding balances by status.",
        "allowed_roles": [_DIRECTOR, _ACCOUNTANT],
        "default_format": "xlsx",
    },
    {
        "key": "ai_usage",
        "title": "AI usage report",
        "description": "AI tokens consumed in the selected month.",
        "allowed_roles": [_DIRECTOR, _HEAD],
        "default_format": "pdf",
    },
    {
        "key": "storage_usage",
        "title": "Storage usage report",
        "description": "Stored file bytes and counts by content library.",
        "allowed_roles": [_DIRECTOR, _HEAD],
        "default_format": "pdf",
    },
]


def seed_reports(apps, schema_editor):
    Report = apps.get_model("reports", "Report")
    for spec in REPORTS:
        Report.objects.update_or_create(
            key=spec["key"],
            defaults={
                "title": spec["title"],
                "description": spec["description"],
                "allowed_roles": spec["allowed_roles"],
                "default_format": spec["default_format"],
            },
        )


def unseed_reports(apps, schema_editor):
    Report = apps.get_model("reports", "Report")
    Report.objects.filter(key__in=[r["key"] for r in REPORTS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0002_report_reportrun_reportschedule_delete_reportitem"),
    ]

    operations = [
        migrations.RunPython(seed_reports, unseed_reports),
    ]
