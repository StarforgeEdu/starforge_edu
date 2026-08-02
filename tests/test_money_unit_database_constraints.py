from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.finance.tests.factories import InvoiceFactory
from apps.payments.tests.factories import PaymentFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("factory", [InvoiceFactory, PaymentFactory])
def test_v1_money_rows_cannot_be_relabelled_through_the_orm(tenant_a, factory):
    with (
        schema_context(tenant_a.schema_name),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        factory(currency="USD")


@pytest.mark.parametrize("factory", [InvoiceFactory, PaymentFactory])
def test_v1_money_rows_accept_their_explicit_uzs_unit(tenant_a, factory):
    with schema_context(tenant_a.schema_name):
        row = factory(currency="UZS")

    assert row.currency == "UZS"
