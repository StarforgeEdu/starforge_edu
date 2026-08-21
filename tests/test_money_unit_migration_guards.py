from __future__ import annotations

import importlib
from unittest import mock

import pytest


@pytest.mark.parametrize(
    ("module_name", "function_name", "model_name"),
    [
        (
            "apps.finance.migrations.0010_invoice_currency_uzs",
            "preflight_invoice_currency",
            "Invoice",
        ),
        (
            "apps.payments.migrations.0009_payment_currency_uzs",
            "preflight_payment_currency",
            "Payment",
        ),
    ],
)
def test_money_unit_migration_preflight_accepts_clean_legacy_rows(
    module_name,
    function_name,
    model_name,
):
    module = importlib.import_module(module_name)
    queryset = mock.Mock()
    queryset.exclude.return_value.count.return_value = 0
    historical_model = mock.Mock()
    historical_model.objects.using.return_value = queryset
    apps = mock.Mock()
    apps.get_model.return_value = historical_model
    schema_editor = mock.Mock()
    schema_editor.connection.alias = "tenant"

    getattr(module, function_name)(apps, schema_editor)

    apps.get_model.assert_called_once_with(
        "finance" if model_name == "Invoice" else "payments",
        model_name,
    )
    historical_model.objects.using.assert_called_once_with("tenant")
    queryset.exclude.assert_called_once_with(currency="UZS")


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    [
        (
            "apps.finance.migrations.0010_invoice_currency_uzs",
            "preflight_invoice_currency",
        ),
        (
            "apps.payments.migrations.0009_payment_currency_uzs",
            "preflight_payment_currency",
        ),
    ],
)
def test_money_unit_migration_preflight_refuses_to_guess_legacy_currency(
    module_name,
    function_name,
):
    module = importlib.import_module(module_name)
    queryset = mock.Mock()
    queryset.exclude.return_value.count.return_value = 4
    historical_model = mock.Mock()
    historical_model.objects.using.return_value = queryset
    apps = mock.Mock()
    apps.get_model.return_value = historical_model
    schema_editor = mock.Mock()
    schema_editor.connection.alias = "tenant"

    with pytest.raises(RuntimeError, match=r"4 .*row\(s\).*non-UZS currency"):
        getattr(module, function_name)(apps, schema_editor)
