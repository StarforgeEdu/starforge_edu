# Data migration (D3-E-2): seed 3 placeholder plans. [OWNER:O-12] real pricing.
# Idempotent via update_or_create on the unique `code` — re-running never
# duplicates, and re-running after an [OWNER:O-12] price edit overwrites the
# placeholder values.

from decimal import Decimal

from django.db import migrations

# Placeholder catalog (O-12 supplies real numbers). max_students drives the
# D3-E-7 enforcement boundary; storage_gb / ai_tokens_month feed metering.
PLANS = [
    {
        "code": "starter",
        "name": "Starter",
        "max_students": 100,
        "max_branches": 1,
        "ai_tokens_month": 100_000,
        "storage_gb": 5,
        "price_uzs": Decimal("0"),
    },
    {
        "code": "standard",
        "name": "Standard",
        "max_students": 500,
        "max_branches": 3,
        "ai_tokens_month": 1_000_000,
        "storage_gb": 50,
        "price_uzs": Decimal("1500000"),
    },
    {
        "code": "pro",
        "name": "Pro",
        "max_students": 5000,
        "max_branches": 20,
        "ai_tokens_month": 10_000_000,
        "storage_gb": 500,
        "price_uzs": Decimal("5000000"),
    },
]


def seed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for spec in PLANS:
        Plan.objects.update_or_create(
            code=spec["code"],
            defaults={
                "name": spec["name"],
                "max_students": spec["max_students"],
                "max_branches": spec["max_branches"],
                "ai_tokens_month": spec["ai_tokens_month"],
                "storage_gb": spec["storage_gb"],
                "price_uzs": spec["price_uzs"],
                "is_active": True,
            },
        )


def unseed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(code__in=[p["code"] for p in PLANS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_plans, unseed_plans),
    ]
