"""Keep HoD access branch-scoped until aggregate sources carry a branch."""

from django.db import migrations


def scope_hod_reports(apps, schema_editor):
    Report = apps.get_model("reports", "Report")
    for report in Report.objects.filter(key__in=("ai_usage", "storage_usage")):
        report.allowed_roles = [role for role in (report.allowed_roles or []) if role != "head_of_dept"]
        report.save(update_fields=["allowed_roles"])


def restore_hod_reports(apps, schema_editor):
    Report = apps.get_model("reports", "Report")
    for report in Report.objects.filter(key__in=("ai_usage", "storage_usage")):
        roles = list(report.allowed_roles or [])
        if "head_of_dept" not in roles:
            roles.append("head_of_dept")
            report.allowed_roles = roles
            report.save(update_fields=["allowed_roles"])


class Migration(migrations.Migration):
    dependencies = [("reports", "0004_reportrun_recipient_ids")]

    operations = [migrations.RunPython(scope_hod_reports, restore_hod_reports)]
