import logging
from decimal import Decimal

from django.db import migrations, models

logger = logging.getLogger("starforge.migrations")


CREATE_SETTINGS_DELETE_GUARD_SQL = r"""
DROP TRIGGER IF EXISTS org_center_settings_reject_delete ON org_centersettings;
CREATE TRIGGER org_center_settings_reject_delete
BEFORE DELETE ON org_centersettings
FOR EACH ROW EXECUTE FUNCTION org_reject_structure_delete();
"""


DROP_SETTINGS_DELETE_GUARD_SQL = r"""
DROP TRIGGER IF EXISTS org_center_settings_reject_delete ON org_centersettings;
"""


CREATE_DISABLED_APPS_VALIDATOR_SQL = r"""
CREATE OR REPLACE FUNCTION org_disabled_apps_valid(candidate jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
BEGIN
    IF candidate IS NULL OR jsonb_typeof(candidate) <> 'array' THEN
        RETURN FALSE;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(candidate) AS entries(item)
        WHERE jsonb_typeof(item) <> 'string'
           OR item #>> '{}' <> ALL (ARRAY[
                'academics', 'access', 'achievements', 'ai', 'approvals',
                'assignments', 'attendance', 'audit', 'campaigns', 'cards',
                'cohorts', 'compliance', 'content', 'covers', 'finance',
                'forms', 'intelligence', 'loans', 'meetings', 'messaging',
                'notifications', 'parents', 'payments', 'placement', 'printing',
                'procurement', 'reports', 'rewards', 'sales', 'schedule',
                'staff_tasks', 'students', 'teachers'
           ]::text[])
    ) THEN
        RETURN FALSE;
    END IF;
    RETURN jsonb_array_length(candidate) = (
        SELECT count(DISTINCT item #>> '{}')
        FROM jsonb_array_elements(candidate) AS entries(item)
    );
END;
$$;
"""


DROP_DISABLED_APPS_VALIDATOR_SQL = r"""
DROP FUNCTION IF EXISTS org_disabled_apps_valid(jsonb);
"""


def preflight_center_settings(apps, schema_editor):
    """Provision the singleton and reject ambiguous or unsafe policy values."""

    from django_tenants.utils import get_public_schema_name

    connection = schema_editor.connection
    if connection.schema_name == get_public_schema_name():
        return
    database = connection.alias
    CenterSettings = apps.get_model("org", "CenterSettings")
    rows = CenterSettings.objects.using(database).order_by("pk")
    row_count = rows.count()
    row_ids = list(rows.values_list("pk", flat=True)[:20])
    if row_count == 0:
        row = CenterSettings.objects.using(database).create(pk=1)
        row_count = 1
        row_ids = [row.pk]
        logger.warning(
            "center settings singleton provisioned during migration: schema=%s",
            connection.schema_name,
        )
    if row_count != 1 or row_ids != [1]:
        raise RuntimeError(
            "center settings preflight failed: expected exactly the pk=1 singleton; "
            f"row_count={row_count}, row_ids={row_ids}"
        )
    row = CenterSettings.objects.using(database).get(pk=1)

    errors: list[str] = []
    if not Decimal("0") <= row.academic_warning_max <= row.honor_roll_min <= Decimal("100"):
        errors.append("grade_thresholds")
    if not Decimal("0") <= row.sibling_discount_percent <= Decimal("100"):
        errors.append("sibling_discount_percent")
    if row.fx_source not in {"cbu", "manual"}:
        errors.append("fx_source")
    if row.fx_rate_usd_manual is not None and row.fx_rate_usd_manual <= 0:
        errors.append("fx_rate_usd_manual")
    if row.fx_source == "manual" and row.fx_rate_usd_manual is None:
        errors.append("manual_fx_missing")
    for field_name in ("currency_primary", "currency_secondary"):
        value = getattr(row, field_name, "")
        if len(value) != 3 or not value.isascii() or not value.isalpha() or value != value.upper():
            errors.append(field_name)
    if row.currency_primary == row.currency_secondary:
        errors.append("duplicate_currency")
    if row.disabled_apps != []:
        # The field is introduced by this migration with an empty default. Any
        # non-empty value would have come from an unexpected concurrent writer.
        errors.append("disabled_apps")
    if errors:
        raise RuntimeError(
            "center settings preflight failed; no policy values were guessed: "
            f"invalid_fields={sorted(set(errors))}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("org", "0020_org_scope_and_history_integrity"),
    ]

    operations = [
        migrations.AddField(
            model_name="centersettings",
            name="disabled_apps",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(preflight_center_settings, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(pk=1),
                name="center_settings_singleton_pk",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(honor_roll_min__gte=0, honor_roll_min__lte=100),
                name="center_settings_honor_roll_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(academic_warning_max__gte=0, academic_warning_max__lte=100),
                name="center_settings_warning_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(academic_warning_max__lte=models.F("honor_roll_min")),
                name="center_settings_grade_threshold_order",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    sibling_discount_percent__gte=0,
                    sibling_discount_percent__lte=100,
                ),
                name="center_settings_sibling_discount_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(fx_source__in=("cbu", "manual")),
                name="center_settings_fx_source_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(fx_rate_usd_manual__isnull=True) | models.Q(fx_rate_usd_manual__gt=0),
                name="center_settings_manual_fx_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=~models.Q(fx_source="manual") | models.Q(fx_rate_usd_manual__isnull=False),
                name="center_settings_manual_fx_present",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(currency_primary__regex=r"^[A-Z]{3}$"),
                name="center_settings_primary_currency_format",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Q(currency_secondary__regex=r"^[A-Z]{3}$"),
                name="center_settings_secondary_currency_format",
            ),
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=~models.Q(currency_primary=models.F("currency_secondary")),
                name="center_settings_currencies_distinct",
            ),
        ),
        migrations.RunSQL(
            CREATE_DISABLED_APPS_VALIDATOR_SQL,
            DROP_DISABLED_APPS_VALIDATOR_SQL,
        ),
        migrations.AddConstraint(
            model_name="centersettings",
            constraint=models.CheckConstraint(
                condition=models.Func(
                    models.F("disabled_apps"),
                    function="org_disabled_apps_valid",
                    output_field=models.BooleanField(),
                ),
                name="center_settings_disabled_apps_valid",
            ),
        ),
        migrations.RunSQL(
            CREATE_SETTINGS_DELETE_GUARD_SQL,
            DROP_SETTINGS_DELETE_GUARD_SQL,
        ),
    ]
