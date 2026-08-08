import pytest
from django.core.cache import cache
from django.db import connection
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/org/settings/"


def test_director_can_read_and_patch_settings(as_role):
    client, _ = as_role(Role.DIRECTOR)
    fetched = client.get(URL)
    assert fetched.status_code == 200
    assert "disabled_apps" not in fetched.json()["data"]
    resp = client.patch(URL, {"late_threshold_minutes": 20}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["late_threshold_minutes"] == 20


def test_general_settings_cannot_read_or_mutate_system_availability(as_role):
    client, _ = as_role(Role.DIRECTOR)
    denied = client.patch(URL, {"disabled_apps": ["placement"]}, format="json")
    assert denied.status_code == 400
    assert set(denied.json()["errors"]) == {"disabled_apps"}

    system = client.get("/api/v1/org/system/apps/")
    assert system.status_code == 200
    assert "apps" in system.json()["data"]


def test_teacher_cannot_read_or_patch_global_settings(as_role):
    """A branch-scoped directory grant must not expose tenant policy knobs."""
    client, _ = as_role(Role.TEACHER)
    assert client.get(URL).status_code == 403
    resp = client.patch(URL, {"late_threshold_minutes": 5}, format="json")
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


def test_settings_rejects_pattern_without_counter(as_role):
    client, _ = as_role(Role.DIRECTOR)
    resp = client.patch(URL, {"student_id_pattern": "STU-{YYYY}"}, format="json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_id_pattern"


def test_settings_rejects_overlong_pattern(as_role):
    client, _ = as_role(Role.DIRECTOR)
    pattern = "X" * 30 + "-{NNNNN}"  # renders to 36 chars > the 32-char column
    resp = client.patch(URL, {"student_id_pattern": pattern}, format="json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_id_pattern"


@pytest.mark.parametrize(
    "payload",
    [
        {"allowed_file_types": "pdf"},  # string, not a list
        {"allowed_file_types": ["not a slug!"]},
        {"otp_channel_prefs": []},  # list, not a dict
        {"otp_channel_prefs": {"pigeon": True}},  # unknown channel
        {"otp_channel_prefs": {"sms": "maybe"}},  # non-boolean value
    ],
)
def test_settings_rejects_malformed_json_knobs(as_role, payload):
    client, _ = as_role(Role.DIRECTOR)
    resp = client.patch(URL, payload, format="json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_settings_rejects_non_string_time_knob(as_role):
    """A non-string JSON value for a time knob (TimeField) must 400, never 500 —
    Django's TimeField.to_python(123) raises a bare TypeError, not ValidationError."""
    client, _ = as_role(Role.DIRECTOR)
    resp = client.patch(URL, {"quiet_hours_start": 123}, format="json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_settings_decimal_echo_is_quantized(as_role):
    """The PATCH echo of a decimal knob is scale-quantized ("90.00"), byte-identical
    to a subsequent GET (DRF decimal-rendering parity)."""
    client, _ = as_role(Role.DIRECTOR)
    resp = client.patch(URL, {"honor_roll_min": 90}, format="json")
    assert resp.status_code == 200
    assert resp.json()["data"]["honor_roll_min"] == "90.00"


def test_settings_accepts_valid_json_knobs(as_role):
    client, _ = as_role(Role.DIRECTOR)
    resp = client.patch(
        URL,
        {"allowed_file_types": ["pdf", "docx"], "otp_channel_prefs": {"sms": True, "email": False}},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["allowed_file_types"] == ["pdf", "docx"]
    assert body["otp_channel_prefs"] == {"sms": True, "email": False}


def test_settings_exposes_and_updates_language_and_absence_knobs(as_role):
    client, _ = as_role(Role.DIRECTOR)
    payload = {
        "default_language": "ru",
        "absence_deduction_enabled": True,
        "absence_deduction_excused_only": True,
    }

    response = client.patch(URL, payload, format="json")
    assert response.status_code == 200, response.content
    assert {key: response.json()["data"][key] for key in payload} == payload
    fetched = client.get(URL).json()["data"]
    assert {key: fetched[key] for key in payload} == payload


def test_settings_reject_unknown_and_inconsistent_policy(as_role):
    client, _ = as_role(Role.DIRECTOR)
    unknown = client.patch(URL, {"otp_cooldown_second": 30}, format="json")
    assert unknown.status_code == 400
    assert "otp_cooldown_second" in unknown.json()["errors"]

    assert (
        client.patch(
            URL,
            {"academic_warning_max": "95", "honor_roll_min": "90"},
            format="json",
        ).status_code
        == 400
    )
    assert client.patch(URL, {"sibling_discount_percent": "101"}, format="json").status_code == 400
    assert client.patch(URL, {"fx_source": "unknown"}, format="json").status_code == 400
    assert (
        client.patch(URL, {"fx_source": "manual", "fx_rate_usd_manual": None}, format="json").status_code
        == 400
    )
    assert (
        client.patch(
            URL,
            {"currency_primary": "usd", "currency_secondary": "USD"},
            format="json",
        ).status_code
        == 400
    )


def test_settings_json_policy_is_bounded_and_normalized(as_role):
    client, _ = as_role(Role.DIRECTOR)
    response = client.patch(
        URL,
        {
            "allowed_file_types": [".PDF", "docx"],
            "otp_channel_prefs": {"sms": True, "email": False},
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["data"]["allowed_file_types"] == ["pdf", "docx"]
    assert (
        client.patch(
            URL,
            {"otp_channel_prefs": {"sms": False, "email": False}},
            format="json",
        ).status_code
        == 400
    )
    assert (
        client.patch(
            URL,
            {"allowed_file_types": ["pdf", "pdf"]},
            format="json",
        ).status_code
        == 400
    )
    unsupported = client.patch(
        URL,
        {"allowed_file_types": ["pdf", "exe"]},
        format="json",
    )
    assert unsupported.status_code == 400
    assert "allowed_file_types" in unsupported.json()["errors"]


def test_settings_and_system_control_require_organization_wide_grants(
    tenant_a,
    as_user,
):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        user = UserFactory()
        account_type = AccountType.objects.create(
            name="Branch policy operator",
            slug="branch-policy-operator",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.bulk_create(
            [
                AccountTypePermission(account_type=account_type, permission=permission)
                for permission in (
                    "organization_settings:read",
                    "organization_settings:write",
                    "system:read",
                    "system:write",
                )
            ]
        )
        RoleMembership.objects.create(
            user=user,
            branch=branch,
            role=Role.SUPPORT,
            account_type=account_type,
        )
        user.refresh_from_db()
    client = as_user(tenant_a, user)

    assert client.get(URL).status_code == 403
    assert client.patch(URL, {"default_language": "uz"}, format="json").status_code == 403
    assert client.get("/api/v1/org/system/apps/").status_code == 403
    assert (
        client.patch(
            "/api/v1/org/system/apps/",
            {"disabled": ["placement"]},
            format="json",
        ).status_code
        == 403
    )


def test_runtime_disabled_apps_survive_cache_loss(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    changed = client.patch(
        "/api/v1/org/system/apps/",
        {"disabled": ["placement"]},
        format="json",
    )
    assert changed.status_code == 200, changed.content
    cache.clear()
    with schema_context(tenant_a.schema_name):
        from core.availability import disabled_apps

        assert "placement" in disabled_apps()
    restored = client.patch(
        "/api/v1/org/system/apps/",
        {"disabled": []},
        format="json",
    )
    assert restored.status_code == 200


def test_missing_settings_fail_closed_without_read_or_partial_write_provisioning(
    as_role,
    tenant_a,
):
    """Lost policy state is an operational fault, never permission to guess defaults."""
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name), connection.cursor() as cursor:
        cursor.execute("SET LOCAL starforge.org_history_maintenance = 'on'")
        cursor.execute("DELETE FROM org_centersettings")
    cache.clear()

    read = client.get(URL)
    assert read.status_code == 503
    assert read.json()["code"] == "configuration_unavailable"
    assert client.patch(URL, {"default_language": "uz"}, format="json").status_code == 503
    assert (
        client.patch(
            "/api/v1/org/system/apps/",
            {"disabled": ["placement"]},
            format="json",
        ).status_code
        == 503
    )
    with schema_context(tenant_a.schema_name):
        from apps.org.models import CenterSettings

        assert CenterSettings.objects.count() == 0
