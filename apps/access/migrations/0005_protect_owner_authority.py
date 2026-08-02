from django.db import migrations, models


PROTECT_OWNER_SQL = r"""
CREATE OR REPLACE FUNCTION access_protect_owner_type()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.is_system AND OLD.slug = 'director' THEN
        RAISE EXCEPTION 'the system owner account type is immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DROP TRIGGER IF EXISTS access_protect_owner_type_trigger ON access_accounttype;
CREATE TRIGGER access_protect_owner_type_trigger
BEFORE UPDATE OR DELETE ON access_accounttype
FOR EACH ROW EXECUTE FUNCTION access_protect_owner_type();

CREATE OR REPLACE FUNCTION access_enforce_reserved_permission()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_is_owner boolean := false;
    new_is_owner boolean := false;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT is_system AND slug = 'director'
          INTO old_is_owner
          FROM access_accounttype
         WHERE id = OLD.account_type_id;

        IF old_is_owner
           AND (OLD.permission = '*:*' OR OLD.permission LIKE 'access:%') THEN
            RAISE EXCEPTION 'the system owner reserved permission is immutable'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT is_system AND slug = 'director'
          INTO new_is_owner
          FROM access_accounttype
         WHERE id = NEW.account_type_id;

        IF NOT COALESCE(new_is_owner, false)
           AND (NEW.permission = '*:*' OR NEW.permission LIKE 'access:%') THEN
            RAISE EXCEPTION 'reserved permissions belong only to the system owner type'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS access_reserved_permission_trigger ON access_accounttypepermission;
CREATE TRIGGER access_reserved_permission_trigger
BEFORE INSERT OR UPDATE OR DELETE ON access_accounttypepermission
FOR EACH ROW EXECUTE FUNCTION access_enforce_reserved_permission();
"""


DROP_OWNER_PROTECTION_SQL = r"""
DROP TRIGGER IF EXISTS access_reserved_permission_trigger ON access_accounttypepermission;
DROP FUNCTION IF EXISTS access_enforce_reserved_permission();
DROP TRIGGER IF EXISTS access_protect_owner_type_trigger ON access_accounttype;
DROP FUNCTION IF EXISTS access_protect_owner_type();
"""


def preflight_reserved_permissions(apps, schema_editor):
    from django_tenants.utils import get_public_schema_name

    if schema_editor.connection.schema_name == get_public_schema_name():
        return

    database = schema_editor.connection.alias
    AccountType = apps.get_model("access", "AccountType")
    AccountTypePermission = apps.get_model("access", "AccountTypePermission")
    RolePermissionOverride = apps.get_model("access", "RolePermissionOverride")

    owners = AccountType.objects.using(database).filter(is_system=True, slug="director")
    if owners.count() != 1:
        raise RuntimeError("access owner-authority preflight failed: expected exactly one system owner type")
    owner = owners.get()
    if not AccountTypePermission.objects.using(database).filter(
        account_type_id=owner.pk,
        permission='*:*',
    ).exists():
        raise RuntimeError("access owner-authority preflight failed: owner wildcard is missing")

    invalid_overrides = RolePermissionOverride.objects.using(database).filter(
        permission__startswith="access:"
    ).count()
    invalid_grants = (
        AccountTypePermission.objects.using(database)
        .filter(models.Q(permission="*:*") | models.Q(permission__startswith="access:"))
        .exclude(account_type_id=owner.pk)
        .count()
    )
    if invalid_overrides or invalid_grants:
        raise RuntimeError(
            "access owner-authority preflight failed: "
            f"reserved_overrides={invalid_overrides}, reserved_non_owner_grants={invalid_grants}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0004_compensation_permissions"),
    ]

    operations = [
        migrations.RunPython(preflight_reserved_permissions, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="rolepermissionoverride",
            constraint=models.CheckConstraint(
                condition=~models.Q(permission__startswith="access:"),
                name="no_access_resource_override",
            ),
        ),
        migrations.RunSQL(PROTECT_OWNER_SQL, DROP_OWNER_PROTECTION_SQL),
    ]
