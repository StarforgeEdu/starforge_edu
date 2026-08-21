from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("academics", "0004_assessment_integrity")]

    operations = [
        migrations.AddField(
            model_name="examresult",
            name="components",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
