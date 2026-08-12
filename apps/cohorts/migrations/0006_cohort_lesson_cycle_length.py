from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cohorts", "0005_finalize_typed_teacher_assignments"),
    ]

    operations = [
        migrations.AddField(
            model_name="cohort",
            name="lesson_cycle_length",
            field=models.PositiveSmallIntegerField(
                choices=[(8, "8 lessons"), (12, "12 lessons")],
                default=12,
            ),
        ),
        migrations.AddConstraint(
            model_name="cohort",
            constraint=models.CheckConstraint(
                condition=models.Q(("lesson_cycle_length__in", (8, 12))),
                name="cohort_lesson_cycle_length_supported",
            ),
        ),
    ]
