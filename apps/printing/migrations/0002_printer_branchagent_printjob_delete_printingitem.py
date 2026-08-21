# Generated for D4-LD-1: replace PrintingItem with Printer/BranchAgent/PrintJob.

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("printing", "0001_initial"),
        ("org", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Printer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="name")),
                ("model_name", models.CharField(blank=True, max_length=120, verbose_name="model name")),
                ("capabilities", models.JSONField(blank=True, default=dict, verbose_name="capabilities")),
                ("is_active", models.BooleanField(default=True, verbose_name="active")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="printers",
                        to="org.branch",
                    ),
                ),
            ],
            options={
                "ordering": ("branch", "name"),
            },
        ),
        migrations.CreateModel(
            name="BranchAgent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="name")),
                ("token_hash", models.CharField(max_length=64, unique=True, verbose_name="token hash")),
                ("last_seen_at", models.DateTimeField(blank=True, null=True, verbose_name="last seen at")),
                ("revoked_at", models.DateTimeField(blank=True, null=True, verbose_name="revoked at")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="print_agents",
                        to="org.branch",
                    ),
                ),
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
            ],
            options={
                "ordering": ("branch", "name"),
            },
        ),
        migrations.CreateModel(
            name="PrintJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("picked", "Picked"),
                            ("printing", "Printing"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=16,
                        verbose_name="status",
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("assignment", "Assignment"),
                            ("transcript", "Transcript"),
                            ("report", "Report"),
                            ("receipt", "Receipt"),
                        ],
                        max_length=16,
                        verbose_name="source",
                    ),
                ),
                ("source_id", models.PositiveBigIntegerField(verbose_name="source id")),
                ("payload_s3_key", models.CharField(max_length=512, verbose_name="payload S3 key")),
                ("pages", models.PositiveIntegerField(verbose_name="pages")),
                ("copies", models.PositiveSmallIntegerField(default=1, verbose_name="copies")),
                ("color", models.BooleanField(default=False, verbose_name="color")),
                ("duplex", models.BooleanField(default=False, verbose_name="duplex")),
                ("cohort_id", models.PositiveBigIntegerField(blank=True, null=True, verbose_name="cohort id")),
                ("attempts", models.PositiveSmallIntegerField(default=0, verbose_name="attempts")),
                (
                    "next_attempt_at",
                    models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="next attempt at"),
                ),
                ("pages_printed", models.PositiveIntegerField(default=0, verbose_name="pages printed")),
                ("last_error", models.TextField(blank=True, verbose_name="last error")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("claimed_at", models.DateTimeField(blank=True, null=True, verbose_name="claimed at")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="finished at")),
                (
                    "agent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="print_jobs",
                        to="printing.branchagent",
                    ),
                ),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="print_jobs",
                        to="org.branch",
                    ),
                ),
                (
                    "printer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="print_jobs",
                        to="printing.printer",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="print_jobs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddConstraint(
            model_name="printer",
            constraint=models.UniqueConstraint(fields=("branch", "name"), name="printer_unique_branch_name"),
        ),
        migrations.AddIndex(
            model_name="branchagent",
            index=models.Index(fields=["token_hash"], name="printing_agent_token_idx"),
        ),
        migrations.AddIndex(
            model_name="printjob",
            index=models.Index(
                fields=["branch", "status", "next_attempt_at"], name="printing_job_claim_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="printjob",
            index=models.Index(fields=["source", "source_id"], name="printing_job_source_idx"),
        ),
        migrations.DeleteModel(
            name="PrintingItem",
        ),
    ]
