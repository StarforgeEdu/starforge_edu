from __future__ import annotations

import re
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django_tenants.utils import get_public_schema_name, schema_context

from apps.audit.models import AuditLog
from apps.org.models import StaffProfile
from apps.org.services import create_staff_account
from apps.org.tests.factories import BranchFactory
from apps.users.models import RoleMembership
from core.permissions import Role

pytestmark = pytest.mark.django_db


def _options(tenant, **overrides):
    values = {
        "schema": tenant.schema_name,
        "branch": "central",
        "username": "admin",
        "first_name": "Amina",
        "last_name": "Director",
        "email": "amina.director@example.com",
    }
    values.update(overrides)
    return values


def test_bootstrap_creates_only_first_director_with_one_time_credentials(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(slug="central")

    stdout = StringIO()
    call_command("bootstrap_tenant_director", **_options(tenant_a), stdout=stdout)
    output = stdout.getvalue()
    password_match = re.search(r"^Temporary password: (\S+)$", output, flags=re.MULTILINE)
    assert password_match is not None
    temporary_password = password_match.group(1)
    assert len(temporary_password) == 20

    with schema_context(tenant_a.schema_name):
        director = StaffProfile.objects.select_related("user").get(username="admin")
        assert director.check_password(temporary_password)
        assert director.must_change_password is True
        assert director.user.has_usable_password() is False
        membership = RoleMembership.objects.get(user=director.user, revoked_at__isnull=True)
        assert membership.branch_id == branch.pk
        assert membership.role == Role.DIRECTOR
        assert membership.account_type is not None
        assert membership.account_type.is_owner_type is True
        event = AuditLog.objects.get(
            action="create",
            resource_type="org.StaffProfile",
            resource_id=str(director.pk),
        )
        assert event.after["bootstrap"] == "first_director"
        assert "password" not in event.after


def test_bootstrap_refuses_when_active_director_already_exists(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory(slug="central")
        create_staff_account(
            branch=branch,
            role=Role.DIRECTOR,
            username="existing.owner",
            email="existing.owner@example.com",
        )

    with pytest.raises(CommandError, match="already has an active director"):
        call_command(
            "bootstrap_tenant_director",
            **_options(tenant_a, username="second.owner", email="second.owner@example.com"),
        )

    with schema_context(tenant_a.schema_name):
        assert not StaffProfile.objects.filter(username="second.owner").exists()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"email": "", "phone": ""}, "recovery contact"),
        ({"branch": "missing"}, "No active, unarchived branch"),
    ],
)
def test_bootstrap_requires_recovery_contact_and_exact_active_branch(tenant_a, overrides, message):
    with schema_context(tenant_a.schema_name):
        BranchFactory(slug="central")

    with pytest.raises(CommandError, match=message):
        call_command("bootstrap_tenant_director", **_options(tenant_a, **overrides))


def test_bootstrap_refuses_public_or_unknown_schema(tenant_a):
    for schema_name in (get_public_schema_name(), "missing_tenant"):
        with pytest.raises(CommandError):
            call_command(
                "bootstrap_tenant_director",
                **_options(tenant_a, schema=schema_name),
            )
