"""Add secure staff uploads, explicit printer routing, and future scheduling."""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("printing", "0005_print_job_delivery_lease"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PrintUploadGrant",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("key", models.CharField(max_length=512, unique=True)),
                ("filename", models.CharField(max_length=255)),
                ("content_type", models.CharField(max_length=127)),
                ("expected_size_bytes", models.PositiveBigIntegerField()),
                ("actual_size_bytes", models.PositiveBigIntegerField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("consumed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("source_deleted_at", models.DateTimeField(blank=True, null=True)),
                ("durable_key", models.CharField(blank=True, max_length=512, null=True, unique=True)),
                ("deletion_requested_at", models.DateTimeField(blank=True, null=True)),
                ("durable_deleted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="print_upload_grants",
                        to="org.branch",
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
        ),
        migrations.AddField(
            model_name="printjob",
            name="preferred_printer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="requested_print_jobs",
                to="printing.printer",
            ),
        ),
        migrations.AddField(
            model_name="printjob",
            name="scheduled_for",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="scheduled for",
            ),
        ),
        migrations.AlterField(
            model_name="printjob",
            name="source",
            field=models.CharField(
                choices=[
                    ("assignment", "Assignment"),
                    ("transcript", "Transcript"),
                    ("report", "Report"),
                    ("receipt", "Receipt"),
                    ("content", "Library content"),
                    ("upload", "Uploaded file"),
                ],
                max_length=16,
                verbose_name="source",
            ),
        ),
        migrations.AddIndex(
            model_name="printuploadgrant",
            index=models.Index(
                fields=["requested_by", "consumed_at", "expires_at"],
                name="print_upload_owner_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="printuploadgrant",
            index=models.Index(
                fields=["source_deleted_at", "expires_at"],
                name="print_upload_source_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="printuploadgrant",
            index=models.Index(
                fields=["durable_deleted_at", "deletion_requested_at"],
                name="print_upload_delete_idx",
            ),
        ),
    ]
