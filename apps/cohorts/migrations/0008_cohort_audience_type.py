from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("cohorts", "0007_cohort_study_month")]

    operations = [
        migrations.AddField(
            model_name="cohort",
            name="audience_type",
            field=models.CharField(
                choices=[
                    ("unspecified", "Needs classification"),
                    ("kids", "Kids"),
                    ("teens", "Teens"),
                    ("adults", "Adults"),
                    ("custom", "Custom / private"),
                ],
                db_index=True,
                default="unspecified",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="cohort",
            name="custom_audience_name",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddConstraint(
            model_name="cohort",
            constraint=models.CheckConstraint(
                condition=(
                    Q(audience_type="custom", custom_audience_name__gt="")
                    | ~Q(audience_type="custom", custom_audience_name="")
                ),
                name="cohort_custom_audience_named",
            ),
        ),
    ]
