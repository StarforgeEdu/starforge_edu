from django.db import migrations, models
from django.db.models import Case, Count, IntegerField, Value, When
from django.utils import timezone

OPEN_STATUSES = ("queued", "picked", "printing")


def close_duplicate_open_jobs(apps, schema_editor):
    PrintJob = apps.get_model("printing", "PrintJob")
    duplicate_keys = (
        PrintJob.objects.filter(status__in=OPEN_STATUSES)
        .values("branch_id", "source", "source_id", "payload_s3_key")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    status_priority = Case(
        When(status="printing", then=Value(0)),
        When(status="picked", then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )

    for key in duplicate_keys.iterator():
        jobs = PrintJob.objects.filter(
            branch_id=key["branch_id"],
            source=key["source"],
            source_id=key["source_id"],
            payload_s3_key=key["payload_s3_key"],
            status__in=OPEN_STATUSES,
        )
        keeper = jobs.order_by(status_priority, "created_at", "pk").first()
        jobs.exclude(pk=keeper.pk).update(
            status="failed",
            last_error="Closed by migration: duplicate open print job.",
            finished_at=timezone.now(),
            next_attempt_at=None,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("printing", "0003_printjob_printjob_created_idx"),
    ]

    operations = [
        migrations.RunPython(close_duplicate_open_jobs, reverse_code=migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="printjob",
            constraint=models.UniqueConstraint(
                fields=("branch", "source", "source_id", "payload_s3_key"),
                condition=models.Q(status__in=("queued", "picked", "printing")),
                name="printing_unique_open_source",
            ),
        ),
    ]
