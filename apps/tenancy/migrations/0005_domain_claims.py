"""Store unverified custom hostnames outside django-tenants' routing table."""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenancy", "0004_alter_platformevent_event")]

    operations = [
        migrations.CreateModel(
            name="DomainClaim",
            fields=[
                (
                    "id",
                    models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
                ),
                ("domain", models.CharField(max_length=253, unique=True)),
                ("verification_token", models.CharField(max_length=64, unique=True)),
                ("pending_primary", models.BooleanField(default=False)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "domain_record",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ownership_claim",
                        to="tenancy.domain",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="domain_claims",
                        to="tenancy.center",
                    ),
                ),
            ],
            options={"ordering": ("domain",)},
        ),
        migrations.AddIndex(
            model_name="domainclaim",
            index=models.Index(fields=["tenant", "created_at"], name="dc_tenant_created_idx"),
        ),
        migrations.AlterField(
            model_name="platformevent",
            name="event",
            field=models.CharField(
                choices=[
                    ("center.suspended", "Center suspended"),
                    ("center.activated", "Center activated"),
                    ("center.trial_extended", "Center trial extended"),
                    ("center.trial_expired", "Center trial expired"),
                    ("center.created", "Center created"),
                    ("center.contact_updated", "Center contact updated"),
                    ("domain.added", "Domain added"),
                    ("domain.verified", "Domain verified"),
                    ("domain.primary_changed", "Primary domain changed"),
                    ("subscription.changed", "Subscription changed"),
                    ("impersonation.minted", "Impersonation token minted"),
                ],
                db_index=True,
                max_length=64,
            ),
        ),
    ]
