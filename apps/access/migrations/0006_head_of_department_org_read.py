from django.db import migrations


def reconcile_head_of_department_org_read(apps, schema_editor):
    """Materialize the scoped organization-directory grant for existing tenants.

    System account-type grants are stored rows, so updating the compatibility
    matrix alone does not affect an installed tenant.  Preserve an explicit
    tenant revoke of either the exact permission or the whole organization
    resource; those remain authoritative policy decisions.
    """

    from django_tenants.utils import get_public_schema_name

    connection = schema_editor.connection
    if connection.schema_name == get_public_schema_name():
        return

    database = connection.alias
    AccountType = apps.get_model("access", "AccountType")
    AccountTypePermission = apps.get_model("access", "AccountTypePermission")
    RolePermissionOverride = apps.get_model("access", "RolePermissionOverride")

    account_type = (
        AccountType.objects.using(database)
        .filter(slug="head_of_dept", is_system=True)
        .first()
    )
    if account_type is None:
        return
    revoked = RolePermissionOverride.objects.using(database).filter(
        role="head_of_dept",
        effect="revoke",
        permission__in=("org:read", "org:*"),
    )
    if revoked.exists():
        return
    AccountTypePermission.objects.using(database).get_or_create(
        account_type_id=account_type.pk,
        permission="org:read",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0005_protect_owner_authority"),
    ]

    operations = [
        migrations.RunPython(
            reconcile_head_of_department_org_read,
            # A reverse delete could remove a tenant-authored grant that existed
            # before this migration, so rollback is intentionally non-destructive.
            migrations.RunPython.noop,
        ),
    ]
