# Generated for D3-E (Lane E — Billing / Paywall). Public-schema only.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("tenancy", "0002_domain_one_primary"),
    ]

    operations = [
        migrations.CreateModel(
            name="Plan",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("code", models.SlugField(max_length=50, unique=True)),
                ("name", models.CharField(max_length=100)),
                ("max_students", models.PositiveIntegerField()),
                ("max_branches", models.PositiveIntegerField()),
                ("ai_tokens_month", models.BigIntegerField()),
                ("storage_gb", models.PositiveIntegerField()),
                ("price_uzs", models.DecimalField(decimal_places=2, max_digits=18)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ("price_uzs",),
            },
        ),
        migrations.CreateModel(
            name="Subscription",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("trialing", "Trialing"),
                            ("active", "Active"),
                            ("past_due", "Past due"),
                            ("suspended", "Suspended"),
                        ],
                        db_index=True,
                        default="trialing",
                        max_length=16,
                    ),
                ),
                ("current_period_start", models.DateTimeField()),
                ("current_period_end", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "center",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="subscription",
                        to="tenancy.center",
                    ),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="subscriptions",
                        to="billing.plan",
                    ),
                ),
            ],
            options={
                "ordering": ("center_id",),
            },
        ),
        migrations.CreateModel(
            name="UsageSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("date", models.DateField()),
                ("students_count", models.PositiveIntegerField(default=0)),
                ("storage_bytes", models.BigIntegerField(default=0)),
                ("ai_tokens_used", models.BigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "center",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="usage_snapshots",
                        to="tenancy.center",
                    ),
                ),
            ],
            options={
                "ordering": ("-date",),
            },
        ),
        migrations.AddConstraint(
            model_name="plan",
            constraint=models.CheckConstraint(
                condition=models.Q(price_uzs__gte=0), name="plan_price_non_negative"
            ),
        ),
        migrations.AddIndex(
            model_name="subscription",
            index=models.Index(fields=["status", "current_period_end"], name="billing_sub_status_end_idx"),
        ),
        migrations.AddConstraint(
            model_name="usagesnapshot",
            constraint=models.UniqueConstraint(
                fields=("center", "date"), name="usage_one_snapshot_per_center_day"
            ),
        ),
        migrations.AddIndex(
            model_name="usagesnapshot",
            index=models.Index(fields=["center", "date"], name="billing_usage_center_date_idx"),
        ),
    ]
