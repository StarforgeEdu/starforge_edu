from django.db import migrations, models
from django.db.models import Q


def keep_one_current_term(apps, schema_editor):
    Term = apps.get_model("schedule", "Term")
    current_ids = list(
        Term.objects.filter(is_current=True)
        .order_by("-start_date", "-updated_at", "-pk")
        .values_list("pk", flat=True)
    )
    if len(current_ids) > 1:
        Term.objects.filter(pk__in=current_ids[1:]).update(is_current=False)


class Migration(migrations.Migration):
    dependencies = [("schedule", "0004_lesson_auto_absence_processed")]

    operations = [
        migrations.RunPython(keep_one_current_term, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="term",
            constraint=models.UniqueConstraint(
                fields=("is_current",),
                condition=Q(is_current=True),
                name="term_one_current",
            ),
        ),
    ]
