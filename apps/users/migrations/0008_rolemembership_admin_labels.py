from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("users", "0007_hash_session_keys_at_rest")]

    operations = [
        migrations.AlterModelOptions(
            name="rolemembership",
            options={
                "ordering": ("-granted_at",),
                "verbose_name": "Account type assignment",
                "verbose_name_plural": "Account type assignments",
            },
        ),
    ]
