from django.db import migrations, models
from django.utils import timezone


def scrub_legacy_integration_payloads(apps, schema_editor):
    WebhookEvent = apps.get_model("payments", "WebhookEvent")
    PaymentAttempt = apps.get_model("payments", "PaymentAttempt")
    ProviderConfig = apps.get_model("payments", "ProviderConfig")
    database = schema_editor.connection.alias

    # Legacy rows contained the complete provider callback and source IP. Neither
    # is required for replay protection, and both can include personal or payment
    # data. The application records only a keyed semantic fingerprint after this
    # migration.
    WebhookEvent.objects.using(database).update(payload={}, remote_ip=None)
    # A deploy interrupts any process that owned an old RECEIVED row. Make those
    # leases immediately retryable; the first authenticated retry binds the new
    # privacy-preserving semantic fingerprint.
    WebhookEvent.objects.using(database).filter(status="received").update(
        status="rejected",
        processed_at=None,
    )
    PaymentAttempt.objects.using(database).update(request_payload={}, response_payload={})
    # The repository's old Uzum HMAC shape is not the current provider API.
    # Prevent a migrated tenant row from advertising an active integration.
    ProviderConfig.objects.using(database).filter(provider="uzum").update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0006_fiscalreceipt_trusted_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="webhookevent",
            name="last_attempted_at",
            field=models.DateTimeField(default=timezone.now),
        ),
        migrations.RunPython(
            scrub_legacy_integration_payloads,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="webhookevent",
            name="remote_ip",
        ),
        migrations.RemoveField(
            model_name="paymentattempt",
            name="request_payload",
        ),
        migrations.RemoveField(
            model_name="paymentattempt",
            name="response_payload",
        ),
    ]
