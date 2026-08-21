from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("compliance", "0005_penalty_penalty_issued_idx")]

    operations = [
        migrations.AlterField(
            model_name="ruleacknowledgment",
            name="rule",
            field=models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="acknowledgments",
                to="compliance.rule",
            ),
        ),
    ]
