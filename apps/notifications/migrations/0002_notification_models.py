# Day-3 Lane C: replace the NotificationItem placeholder with the real
# notification substrate (Notification / NotificationDelivery /
# NotificationPreference / NotificationTemplate).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.DeleteModel(name="NotificationItem"),
        migrations.CreateModel(
            name="Notification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("attendance.absent", "Attendance: absent"),
                            ("attendance.late", "Attendance: late"),
                            ("academics.grades_published", "Academics: grades published"),
                            ("assignments.created", "Assignment created"),
                            ("assignments.due_soon", "Assignment due soon"),
                            ("assignments.graded", "Assignment graded"),
                            ("schedule.lesson_reminder", "Lesson reminder"),
                            ("auth.new_device_login", "New device login"),
                            ("students.enrollment_changed", "Enrollment changed"),
                            ("finance.invoice_issued", "Invoice issued"),
                            ("finance.payment_reminder", "Payment reminder"),
                            ("payments.payment_completed", "Payment completed"),
                            ("payments.payment_failed", "Payment failed"),
                            ("cohorts.announcement", "Cohort announcement"),
                            ("billing.subscription_past_due", "Subscription past due"),
                            ("billing.subscription_suspended", "Subscription suspended"),
                        ],
                        db_index=True,
                        max_length=64,
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                ("body", models.TextField(blank=True)),
                ("data", models.JSONField(blank=True, default=dict)),
                ("dedupe_key", models.CharField(blank=True, max_length=128, null=True, unique=True)),
                ("read_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="NotificationDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "channel",
                    models.CharField(
                        choices=[("sms", "SMS"), ("email", "Email"), ("push", "Push"), ("in_app", "In-app")],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                            ("skipped_pref", "Skipped (preference off)"),
                            ("skipped_quiet_hours", "Skipped (quiet hours)"),
                            ("dead_token", "Dead push token"),
                        ],
                        db_index=True,
                        max_length=24,
                    ),
                ),
                ("provider_response", models.JSONField(blank=True, default=dict)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "notification",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deliveries",
                        to="notifications.notification",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="NotificationPreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(max_length=64)),
                ("channel", models.CharField(max_length=16)),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_preferences",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("user", "event_type", "channel")},
        ),
        migrations.CreateModel(
            name="NotificationTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(db_index=True, max_length=64)),
                ("channel", models.CharField(max_length=16)),
                ("locale", models.CharField(choices=[("uz", "Uzbek"), ("ru", "Russian"), ("en", "English")], max_length=2)),
                ("subject", models.CharField(blank=True, max_length=255)),
                ("body", models.TextField()),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("event_type", "channel", "locale")},
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(fields=["user", "read_at"], name="notif_user_read_idx"),
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(fields=["user", "-created_at"], name="notif_user_created_idx"),
        ),
        migrations.AddIndex(
            model_name="notificationdelivery",
            index=models.Index(fields=["notification", "channel"], name="notif_delivery_chan_idx"),
        ),
        migrations.AddIndex(
            model_name="notificationpreference",
            index=models.Index(fields=["user", "event_type"], name="notif_pref_user_event_idx"),
        ),
        migrations.AddConstraint(
            model_name="notificationpreference",
            constraint=models.UniqueConstraint(
                fields=("user", "event_type", "channel"),
                name="notif_pref_unique_user_event_channel",
            ),
        ),
        migrations.AddIndex(
            model_name="notificationtemplate",
            index=models.Index(fields=["event_type", "channel", "locale"], name="notif_tmpl_evt_chan_loc_idx"),
        ),
        migrations.AddConstraint(
            model_name="notificationtemplate",
            constraint=models.UniqueConstraint(
                fields=("event_type", "channel", "locale"),
                name="notif_template_unique_event_channel_locale",
            ),
        ),
    ]
