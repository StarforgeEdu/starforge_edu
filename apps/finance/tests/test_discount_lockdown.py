"""Discounts are granted ONLY through the Approvals engine (the `discount` KIND).

The finance Discount endpoint must therefore not be a back door: no direct
create / edit / delete (which would side-step the approval gate and mutate the
audited discount out-of-band). It stays read-only over CRUD; a standing discount
can only be ENDED via the explicit `deactivate` action."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.finance.tests.factories import DiscountFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

URL = "/api/v1/finance/discounts/"


def _discount_id(tenant, **kwargs) -> int:
    with schema_context(tenant.schema_name):
        return DiscountFactory.create(**kwargs).id


def test_direct_create_is_blocked(tenant_a, as_role):
    # An accountant holds finance:write but still cannot mint a discount directly —
    # it must come from an approved discount request.
    accountant, _ = as_role(Role.ACCOUNTANT)
    sid = None
    with schema_context(tenant_a.schema_name):
        from apps.students.tests.factories import StudentProfileFactory

        sid = StudentProfileFactory.create().id
    resp = accountant.post(URL, {"student": sid, "discount_type": "manual", "percent": "10"}, format="json")
    assert resp.status_code == 405, resp.content


def test_edit_and_delete_are_blocked(tenant_a, as_role):
    accountant, _ = as_role(Role.ACCOUNTANT)
    did = _discount_id(tenant_a)
    assert accountant.put(f"{URL}{did}/", {"percent": "5"}, format="json").status_code == 405
    assert accountant.patch(f"{URL}{did}/", {"percent": "5"}, format="json").status_code == 405
    assert accountant.delete(f"{URL}{did}/").status_code == 405


def test_deactivate_ends_a_discount(tenant_a, as_role):
    accountant, _ = as_role(Role.ACCOUNTANT)
    did = _discount_id(tenant_a, is_active=True)
    resp = accountant.post(f"{URL}{did}/deactivate/", {}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["data"]["is_active"] is False
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        assert Discount.objects.get(pk=did).is_active is False


def test_deactivate_requires_finance_write(tenant_a, as_role):
    # A cashier has finance:read but not finance:write -> cannot end a discount.
    cashier, _ = as_role(Role.CASHIER)
    did = _discount_id(tenant_a, is_active=True)
    assert cashier.post(f"{URL}{did}/deactivate/", {}, format="json").status_code == 403
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        assert Discount.objects.get(pk=did).is_active is True  # untouched


def test_read_still_works(tenant_a, as_role):
    accountant, _ = as_role(Role.ACCOUNTANT)
    did = _discount_id(tenant_a)
    listing = accountant.get(URL)
    assert listing.status_code == 200
    assert any(row["id"] == did for row in listing.json()["data"])
    assert accountant.get(f"{URL}{did}/").status_code == 200
