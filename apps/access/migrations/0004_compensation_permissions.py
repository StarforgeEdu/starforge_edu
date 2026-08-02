from django.db import migrations

ROLE_PERMISSIONS = {
    "accountant": frozenset(
        {
            "compensation:read",
            "compensation:write",
            "compensation:run",
            "compensation:approve",
            "compensation:disburse",
        }
    ),
    "cashier": frozenset({"compensation:disburse"}),
}


def reconcile_compensation_permissions(apps, schema_editor):
    """Seed the new, explicit pay boundary on canonical system types only.

    Existing ``finance:*`` grants are deliberately not translated: doing so
    would preserve the privacy bug this migration closes.  Exact/resource-wide
    compatibility revocations remain authoritative for the system roles.
    """
    from django_tenants.utils import get_public_schema_name

    connection = schema_editor.connection
    if connection.schema_name == get_public_schema_name():
        return

    database = connection.alias
    AccountType = apps.get_model("access", "AccountType")
    AccountTypePermission = apps.get_model("access", "AccountTypePermission")
    RolePermissionOverride = apps.get_model("access", "RolePermissionOverride")

    for role, role_permissions in ROLE_PERMISSIONS.items():
        # Inactive system types still need the schema upgrade.  Skipping them
        # leaves a latent privilege/availability defect when an operator later
        # reactivates the canonical accountant or cashier type.
        account_type = AccountType.objects.using(database).filter(slug=role, is_system=True).first()
        if account_type is None:
            continue

        revoked = set(
            RolePermissionOverride.objects.using(database)
            .filter(
                role=role,
                effect="revoke",
                permission__in=(*role_permissions, "compensation:*"),
            )
            .values_list("permission", flat=True)
        )
        allowed = {
            permission
            for permission in role_permissions
            if "compensation:*" not in revoked and permission not in revoked
        }
        AccountTypePermission.objects.using(database).bulk_create(
            [
                AccountTypePermission(
                    account_type_id=account_type.pk,
                    permission=permission,
                )
                for permission in sorted(allowed)
            ],
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0003_registrar_safeguarding_permissions"),
    ]

    operations = [
        migrations.RunPython(
            reconcile_compensation_permissions,
            # Do not remove indistinguishable tenant-authored grants on rollback.
            migrations.RunPython.noop,
        ),
    ]
