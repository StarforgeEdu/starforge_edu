from django.db import migrations, models

import core.validators


class Migration(migrations.Migration):
    dependencies = [
        ("org", "0018_staffprofile_staff_phone_unique_nonblank_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="centersettings",
            name="organization_timezone",
            field=models.CharField(
                default="Asia/Tashkent",
                help_text="IANA timezone used for organization-wide business dates.",
                max_length=64,
                validators=[core.validators.validate_iana_timezone],
            ),
        ),
    ]
