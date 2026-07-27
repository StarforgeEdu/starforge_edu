"""PlatformEvent — append-only platform control-center audit trail (D4-LE-5).

Public-schema only (apps.tenancy is in SHARED_APPS). FK to the SHARED users.User
(platform staff, TD-3) and to Center. No update/delete API — immutable once
written.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0002_domain_one_primary"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PlatformEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "event",
                    models.CharField(
                        choices=[
                            ("center.suspended", "Center suspended"),
                            ("center.activated", "Center activated"),
                            ("center.trial_extended", "Center trial extended"),
                            ("center.created", "Center created"),
                            ("subscription.changed", "Subscription changed"),
                            ("impersonation.minted", "Impersonation token minted"),
                        ],
                        db_index=True,
                        max_length=64,
                    ),
                ),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="platform_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "center",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="platform_events",
                        to="tenancy.center",
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="platformevent",
            index=models.Index(fields=["center", "created_at"], name="pe_center_created_idx"),
        ),
        migrations.AddIndex(
            model_name="platformevent",
            index=models.Index(fields=["event", "created_at"], name="pe_event_created_idx"),
        ),
    ]
