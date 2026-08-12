from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("attendance", "0003_attendancerecord_attnrec_created_idx")]

    operations = [
        migrations.AddField(
            model_name="attendancerecord",
            name="card_type",
            field=models.CharField(
                blank=True,
                choices=[("", "No card"), ("smart", "Smart card"), ("warning", "Warning card")],
                default="",
                max_length=8,
            ),
        ),
        migrations.AddConstraint(
            model_name="attendancerecord",
            constraint=models.CheckConstraint(
                condition=models.Q(("card_type__in", ("", "smart", "warning"))),
                name="attendance_card_type_valid",
            ),
        ),
    ]
