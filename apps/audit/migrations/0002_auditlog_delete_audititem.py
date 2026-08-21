"""Audit trail schema (D3-D-1) — replaces the AuditItem placeholder.

APPEND-ONLY INVARIANT (TD-9, D3-D-5):
    Application code NEVER updates or deletes ``audit_auditlog`` rows. The only
    writer is an INSERT (``apps.audit.services.audit_log`` + the post_save/
    post_delete receivers). The single deleter is the age-based retention task
    ``celery_tasks.audit_tasks.cleanup_old_audit_logs`` (7y for finance/payments/
    refund/grade/examresult resource types, 1y otherwise). There is no
    ``updated_at`` column and no update code path.

PRODUCTION HARDENING (runbook line; actual grant is [OWNER:O-9] hosting):
    The application DB role should additionally be locked down at the DB level so
    a compromised app cannot rewrite history:

        REVOKE UPDATE, DELETE ON audit_auditlog FROM <app_role>;

    Run retention from a separate maintenance role that retains DELETE. This
    migration intentionally does NOT issue the REVOKE itself: in this repo the
    same role runs migrations, app traffic, and the retention task, so a REVOKE
    here would break retention. The grant split is an ops/hosting decision.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("actor_repr", models.CharField(blank=True, max_length=255)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("create", "Create"),
                            ("update", "Update"),
                            ("delete", "O'chirish"),
                            ("login", "Login"),
                            ("login_failed", "Login failed"),
                            ("logout", "Logout"),
                            ("otp_request", "OTP request"),
                            ("otp_verify", "OTP verify"),
                            ("impersonate", "Impersonate"),
                            ("export", "Export"),
                        ],
                        db_index=True,
                        max_length=32,
                    ),
                ),
                ("resource_type", models.CharField(blank=True, max_length=100)),
                ("resource_id", models.CharField(blank=True, max_length=64)),
                ("before", models.JSONField(blank=True, null=True)),
                ("after", models.JSONField(blank=True, null=True)),
                ("ip", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=512)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "actor",
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
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(
                fields=["resource_type", "resource_id"], name="audit_audit_resourc_2a3aef_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["actor"], name="audit_audit_actor_i_17b775_idx"),
        ),
        migrations.DeleteModel(
            name="AuditItem",
        ),
    ]
