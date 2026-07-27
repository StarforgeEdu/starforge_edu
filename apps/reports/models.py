"""Reports domain models (TASKS §20, D4-LB-1).

A ``Report`` is one row in the tenant's report *library* (six seeded rows: one
per generator key). A ``ReportRun`` is a single one-shot generation, rendered
off-request by ``celery_tasks.report_tasks.build_report`` to PDF/Excel, uploaded
to S3, and delivered as a signed URL via ``notifications.dispatch``. A
``ReportSchedule`` fires runs on a weekly/monthly cadence (hourly beat scan with
a ``last_run_at`` guard).

All three live in the tenant schema. The cross-tenant nightly aggregation that
fills ``billing.UsageSnapshot`` is a public-schema Celery task, not a model here.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class ReportKey(models.TextChoices):
    """The six generator keys — one library Report per key, seeded per tenant."""

    ENROLLMENT = "enrollment", _("Enrollment")
    ATTENDANCE = "attendance", _("Attendance")
    GRADES = "grades", _("Grades")
    FINANCE = "finance", _("Finance")
    AI_USAGE = "ai_usage", _("AI usage")
    STORAGE_USAGE = "storage_usage", _("Storage usage")


class ReportFormat(models.TextChoices):
    PDF = "pdf", _("PDF")
    XLSX = "xlsx", _("Excel")


class Report(models.Model):
    """A library entry: metadata + role visibility for one generator key."""

    key = models.CharField(max_length=32, choices=ReportKey.choices, unique=True)
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    # List of core.permissions.Role codes allowed to run/see this report.
    allowed_roles = models.JSONField(default=list)
    default_format = models.CharField(max_length=8, choices=ReportFormat.choices, default=ReportFormat.PDF)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("key",)
        verbose_name = _("report")
        verbose_name_plural = _("reports")

    def __str__(self) -> str:  # pragma: no cover
        return self.key


class ReportRun(models.Model):
    """One generation of a Report. queued → running → done | failed."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        RUNNING = "running", _("Running")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    report = models.ForeignKey(Report, on_delete=models.PROTECT, related_name="runs")
    requested_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    params = models.JSONField(default=dict, blank=True)
    # Extra recipients (user ids) the ready notification is delivered to, beyond
    # the requester. Copied from ReportSchedule.recipient_ids on a scheduled run;
    # empty for an on-request run.
    recipient_ids = models.JSONField(default=list, blank=True)
    format = models.CharField(max_length=8, choices=ReportFormat.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    s3_key = models.CharField(max_length=512, blank=True)
    file_bytes = models.PositiveBigIntegerField(default=0)
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("report", "status"), name="reports_run_report_status_idx"),
            models.Index(fields=("status", "created_at"), name="reports_run_status_created_idx"),
        ]
        verbose_name = _("report run")
        verbose_name_plural = _("report runs")

    def __str__(self) -> str:  # pragma: no cover
        return f"run#{self.pk}:{self.report_id}:{self.status}"


class ReportSchedule(models.Model):
    """A recurring report run. The hourly beat scan fires due rows once per
    cadence window (``last_run_at`` guard)."""

    class Cadence(models.TextChoices):
        WEEKLY = "weekly", _("Weekly")
        MONTHLY = "monthly", _("Monthly")

    report = models.ForeignKey(Report, on_delete=models.PROTECT, related_name="schedules")
    cadence = models.CharField(max_length=16, choices=Cadence.choices)
    # weekly => weekday in 0..6 (Mon=0); monthly => day_of_month in 1..31.
    weekday = models.PositiveSmallIntegerField(null=True, blank=True)
    day_of_month = models.PositiveSmallIntegerField(null=True, blank=True)
    hour = models.PositiveSmallIntegerField(default=7)
    format = models.CharField(max_length=8, choices=ReportFormat.choices, default=ReportFormat.PDF)
    params = models.JSONField(default=dict, blank=True)
    recipient_ids = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("is_active", "cadence"), name="reports_sched_active_cad_idx"),
        ]
        constraints = [
            # weekly => weekday set; monthly => day_of_month set.
            models.CheckConstraint(
                condition=(
                    models.Q(cadence="weekly", weekday__isnull=False)
                    | models.Q(cadence="monthly", day_of_month__isnull=False)
                ),
                name="report_schedule_cadence_anchor",
            ),
        ]
        verbose_name = _("report schedule")
        verbose_name_plural = _("report schedules")

    def __str__(self) -> str:  # pragma: no cover
        return f"schedule#{self.pk}:{self.report_id}:{self.cadence}"
