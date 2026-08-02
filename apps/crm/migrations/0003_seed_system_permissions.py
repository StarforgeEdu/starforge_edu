from django.db import migrations


CRM_DEFAULT_GRANTS = {
    "head_of_dept": ("crm:read", "crm:write"),
    "registrar": ("crm:read", "crm:write"),
}


def add_crm_grants(apps, schema_editor):
    AccountType = apps.get_model("access", "AccountType")
    AccountTypePermission = apps.get_model("access", "AccountTypePermission")
    database = schema_editor.connection.alias
    for slug, grants in CRM_DEFAULT_GRANTS.items():
        account_type = (
            AccountType.objects.using(database)
            .filter(is_system=True, slug=slug, is_active=True)
            .first()
        )
        if account_type is None:
            continue
        AccountTypePermission.objects.using(database).bulk_create(
            [
                AccountTypePermission(account_type=account_type, permission=permission)
                for permission in grants
            ],
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0006_head_of_department_org_read"),
        ("crm", "0002_seed_and_immutable_history"),
    ]

    # A reverse migration cannot distinguish grants inserted above from grants
    # an operator had already configured explicitly. Preserve them rather than
    # silently revoking live authority during a rollback.
    operations = [migrations.RunPython(add_crm_grants, migrations.RunPython.noop)]
