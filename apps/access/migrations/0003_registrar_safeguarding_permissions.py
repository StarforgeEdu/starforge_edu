from django.db import migrations

SAFEGUARDING_PERMISSIONS = frozenset(
    {
        "safeguarding:read",
        "safeguarding:write",
    }
)


def reconcile_registrar_safeguarding_permissions(apps, schema_editor):
    """Backfill grants introduced after the system account types were seeded.

    System account types are materialized per tenant, so changing the static
    role matrix alone does not update an existing installation.  Respect an
    exact or resource-wide legacy revoke: those rows express an intentional
    tenant policy and must continue to deny the matching capability.
    """
    from django_tenants.utils import get_public_schema_name

    connection = schema_editor.connection
    if connection.schema_name == get_public_schema_name():
        return

    database = connection.alias
    AccountType = apps.get_model("access", "AccountType")
    AccountTypePermission = apps.get_model("access", "AccountTypePermission")
    RolePermissionOverride = apps.get_model("access", "RolePermissionOverride")

    registrar = AccountType.objects.using(database).filter(slug="registrar", is_system=True).first()
    if registrar is None:
        return

    revoked = set(
        RolePermissionOverride.objects.using(database)
        .filter(
            role="registrar",
            effect="revoke",
            permission__in=(*SAFEGUARDING_PERMISSIONS, "safeguarding:*"),
        )
        .values_list("permission", flat=True)
    )
    permissions = {
        permission
        for permission in SAFEGUARDING_PERMISSIONS
        if "safeguarding:*" not in revoked and permission not in revoked
    }

    AccountTypePermission.objects.using(database).bulk_create(
        [
            AccountTypePermission(
                account_type_id=registrar.pk,
                permission=permission,
            )
            for permission in sorted(permissions)
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0002_accounttype_accounttypepermission"),
    ]

    operations = [
        migrations.RunPython(
            reconcile_registrar_safeguarding_permissions,
            # Existing rows are indistinguishable from rows created here. A
            # reverse delete could therefore remove a tenant's pre-existing,
            # intentional grants; keep rollback non-destructive.
            migrations.RunPython.noop,
        ),
    ]
