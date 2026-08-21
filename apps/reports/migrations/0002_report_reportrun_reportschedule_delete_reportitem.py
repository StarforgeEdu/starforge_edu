"""D4-LB-1: replace the ReportItem placeholder with the real report library
models (Report / ReportRun / ReportSchedule). Tenant-schema only."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Report",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "key",
                    models.CharField(
                        choices=[
                            ("enrollment", "Enrollment"),
                            ("attendance", "Attendance"),
                            ("grades", "Grades"),
                            ("finance", "Finance"),
                            ("ai_usage", "AI usage"),
                            ("storage_usage", "Storage usage"),
                        ],
                        max_length=32,
                        unique=True,
                    ),
                ),
                ("title", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                ("allowed_roles", models.JSONField(default=list)),
                (
                    "default_format",
                    models.CharField(
                        choices=[("pdf", "PDF"), ("xlsx", "Excel")], default="pdf", max_length=8
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "report",
                "verbose_name_plural": "reports",
                "ordering": ("key",),
            },
        ),
        migrations.CreateModel(
            name="ReportRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("params", models.JSONField(blank=True, default=dict)),
                ("format", models.CharField(choices=[("pdf", "PDF"), ("xlsx", "Excel")], max_length=8)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("s3_key", models.CharField(blank=True, max_length=512)),
                ("file_bytes", models.PositiveBigIntegerField(default=0)),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="runs",
                        to="reports.report",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "report run",
                "verbose_name_plural": "report runs",
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="ReportSchedule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cadence", models.CharField(choices=[("weekly", "Weekly"), ("monthly", "Monthly")], max_length=16)),
                ("weekday", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("day_of_month", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("hour", models.PositiveSmallIntegerField(default=7)),
                (
                    "format",
                    models.CharField(
                        choices=[("pdf", "PDF"), ("xlsx", "Excel")], default="pdf", max_length=8
                    ),
                ),
                ("params", models.JSONField(blank=True, default=dict)),
                ("recipient_ids", models.JSONField(blank=True, default=list)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("last_run_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="schedules",
                        to="reports.report",
                    ),
                ),
            ],
            options={
                "verbose_name": "report schedule",
                "verbose_name_plural": "report schedules",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="reportrun",
            index=models.Index(fields=["report", "status"], name="reports_run_report_status_idx"),
        ),
        migrations.AddIndex(
            model_name="reportrun",
            index=models.Index(fields=["status", "created_at"], name="reports_run_status_created_idx"),
        ),
        migrations.AddIndex(
            model_name="reportschedule",
            index=models.Index(fields=["is_active", "cadence"], name="reports_sched_active_cad_idx"),
        ),
        migrations.AddConstraint(
            model_name="reportschedule",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(cadence="weekly", weekday__isnull=False)
                    | models.Q(cadence="monthly", day_of_month__isnull=False)
                ),
                name="report_schedule_cadence_anchor",
            ),
        ),
        migrations.DeleteModel(name="ReportItem"),
    ]
