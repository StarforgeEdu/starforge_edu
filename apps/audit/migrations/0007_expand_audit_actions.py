from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("audit", "0006_actor_principal_snapshot")]

    operations = [
        migrations.AlterField(
            model_name="auditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("create", "Create"),
                    ("update", "Update"),
                    ("delete", "Delete"),
                    ("login", "Login"),
                    ("login_failed", "Login failed"),
                    ("logout", "Logout"),
                    ("otp_request", "OTP request"),
                    ("otp_verify", "OTP verify"),
                    ("impersonate", "Impersonate"),
                    ("export", "Export"),
                    ("export.complete", "Export completed"),
                    ("export.failed", "Export failed"),
                    ("session.revoked", "Session revoked"),
                    ("print.job_created", "Print job created"),
                    ("print.job_rejected", "Print job rejected"),
                    ("print.job_done", "Print job completed"),
                    ("print.job_failed", "Print job failed"),
                    ("print.job_retry_scheduled", "Print job retry scheduled"),
                    (
                        "print.job_reconciliation_required",
                        "Print job reconciliation required",
                    ),
                    ("print.job_reconciled", "Print job reconciled"),
                ],
                db_index=True,
                max_length=64,
            ),
        ),
    ]
