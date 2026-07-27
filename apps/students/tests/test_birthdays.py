"""/students/birthdays/ query-param hardening: bounded ?days (worker DoS) and
typed ?branch/?cohort (no ValueError 500s) — all errors in the TD-18 envelope."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.users.tests.factories import UserFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/students/birthdays/"


def _birthdate_in(days: int):
    """A birthdate whose month/day lands `days` from today (year is irrelevant)."""
    target = timezone.now().date() + timedelta(days=days)
    try:
        return target.replace(year=2010)
    except ValueError:  # Feb 29 target in a non-leap birth year
        return target.replace(year=2010, day=28)


def test_birthdays_days_over_cap_400(as_role):
    client, _ = as_role(Role.DIRECTOR)
    resp = client.get(URL, {"days": 2_000_000})
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


@pytest.mark.parametrize("params", [{"branch": "abc"}, {"cohort": "abc"}, {"days": "abc"}])
def test_birthdays_non_numeric_params_400(as_role, params):
    client, _ = as_role(Role.DIRECTOR)
    resp = client.get(URL, params)
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_birthdays_window_filters(as_role, tenant_a):
    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        soon = StudentProfileFactory.create(
            branch=branch, user=UserFactory.create(birthdate=_birthdate_in(3))
        )
        far = StudentProfileFactory.create(
            branch=branch, user=UserFactory.create(birthdate=_birthdate_in(40))
        )

    body = client.get(URL, {"days": 7}).json()
    ids = [s["id"] for s in body["data"]]
    assert soon.id in ids
    assert far.id not in ids
