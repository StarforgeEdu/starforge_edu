from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("reports", "0005_scope_hod_aggregate_reports")]

    operations = [
        # Tenant schemas are also migrated while a new Center is provisioned
        # inside its creation transaction. PostgreSQL concurrent index creation
        # cannot run there, so tenant-app migrations must use transaction-safe
        # AddIndex operations. Large existing installations can still schedule
        # any additional online index maintenance as a separate release action.
        migrations.AddIndex(
            model_name="reportrun",
            index=GinIndex(fields=["params"], name="report_run_params_gin"),
        ),
        migrations.AddIndex(
            model_name="reportschedule",
            index=GinIndex(fields=["params"], name="report_sched_params_gin"),
        ),
    ]
