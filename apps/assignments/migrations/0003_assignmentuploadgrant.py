from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("assignments", "0002_assignment_submission_submissiongrade_and_more"),
        ("users", "0004_rolemembership_unique_null_department"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssignmentUploadGrant",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("key", models.CharField(max_length=512, unique=True)),
                ("content_type", models.CharField(max_length=127)),
                ("expected_size_bytes", models.PositiveBigIntegerField()),
                ("actual_size_bytes", models.PositiveBigIntegerField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("consumed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="+", to="users.user"
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="assignmentuploadgrant",
            index=models.Index(
                fields=["requested_by", "consumed_at", "expires_at"],
                name="assignments_request_907163_idx",
            ),
        ),
    ]
