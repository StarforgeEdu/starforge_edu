from django.db import migrations, models
from django.db.models import Count


def deduplicate_null_department_memberships(apps, schema_editor):
    RoleMembership = apps.get_model("users", "RoleMembership")
    duplicate_keys = (
        RoleMembership.objects.filter(department_id__isnull=True)
        .values("user_id", "branch_id", "role")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )

    for key in duplicate_keys.iterator():
        memberships = RoleMembership.objects.filter(
            user_id=key["user_id"],
            branch_id=key["branch_id"],
            department_id__isnull=True,
            role=key["role"],
        )
        keeper = memberships.filter(revoked_at__isnull=True).order_by("granted_at", "pk").first()
        if keeper is None:
            keeper = memberships.order_by("-granted_at", "pk").first()
        memberships.exclude(pk=keeper.pk).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0003_session_principal"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_null_department_memberships,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="rolemembership",
            constraint=models.UniqueConstraint(
                fields=("user", "branch", "role"),
                condition=models.Q(department__isnull=True),
                name="role_membership_unique_branch_role",
            ),
        ),
    ]
