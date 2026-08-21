from django.db import migrations, models
import django.db.models.deletion


def assert_unambiguous_file_locations(apps, schema_editor):
    """Block the release instead of guessing how to repair an ambiguous row."""

    LessonFile = apps.get_model("content", "LessonFile")
    invalid = LessonFile.objects.using(schema_editor.connection.alias).filter(
        models.Q(lesson__isnull=True, folder__isnull=True)
        | models.Q(lesson__isnull=False, folder__isnull=False)
    )
    if invalid.exists():
        raise RuntimeError(
            "Content migration blocked: LessonFile rows must reference exactly one lesson or folder; "
            f"review {invalid.count()} ambiguous row(s) before retrying."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0005_folder_unique_root_name"),
        ("teachers", "0010_alter_payoutpolicy_method"),
    ]

    operations = [
        migrations.RunPython(assert_unambiguous_file_locations, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="lessonfile",
            name="lessonfile_lesson_or_folder",
        ),
        migrations.AddConstraint(
            model_name="lessonfile",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(lesson__isnull=False, folder__isnull=True)
                    | models.Q(lesson__isnull=True, folder__isnull=False)
                ),
                name="lessonfile_lesson_or_folder",
            ),
        ),
        migrations.AddField(
            model_name="lessonfile",
            name="submission_audience",
            field=models.CharField(
                blank=True,
                choices=[("own_students", "Own students"), ("global", "Everyone in the center")],
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="lessonfile",
            name="submitted_by_teacher",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="content_submissions",
                to="teachers.teacherprofile",
            ),
        ),
        migrations.AddConstraint(
            model_name="lessonfile",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("submission_audience", ""), ("submitted_by_teacher__isnull", True))
                    | models.Q(
                        ("submission_audience__in", ("own_students", "global")),
                        ("submitted_by_teacher__isnull", False),
                    )
                ),
                name="content_file_submission_audience_shape",
            ),
        ),
        migrations.AddIndex(
            model_name="lessonfile",
            index=models.Index(
                fields=["submitted_by_teacher", "submission_audience"],
                name="content_teacher_audience_idx",
            ),
        ),
    ]
