from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cohorts", "0006_cohort_lesson_cycle_length")]

    operations = [
        migrations.AddField(
            model_name="cohort",
            name="study_month",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddConstraint(
            model_name="cohort",
            constraint=models.CheckConstraint(
                condition=models.Q(study_month__gte=1, study_month__lte=600),
                name="cohort_study_month_supported",
            ),
        ),
    ]
