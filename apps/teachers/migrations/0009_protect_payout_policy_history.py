from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("teachers", "0008_teachertype"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payoutpolicy",
            name="teacher",
            field=models.OneToOneField(
                on_delete=models.PROTECT,
                related_name="payout_policy",
                to="teachers.teacherprofile",
            ),
        ),
    ]
