from __future__ import annotations

from unittest import mock

import pytest
from django.db import connection

from apps.tenancy.management.commands import check_release_migration_preflight as preflight


@pytest.mark.parametrize("target", sorted(preflight._APPROVED_MODEL_TARGETS))
def test_release_preflight_model_inventory_resolves_to_quoted_identifiers(target):
    app_label, model_name = target
    model = preflight.django_apps.get_model(app_label, model_name)
    reference = preflight._table(app_label, model_name)

    assert reference.name == model._meta.db_table
    assert str(reference) == connection.ops.quote_name(model._meta.db_table)


@pytest.mark.parametrize(
    ("app_label", "model_name"),
    [
        ("unknown", "Model"),
        ("payments", 'Payment"; DROP TABLE payments_payment; --'),
    ],
)
def test_release_preflight_rejects_unknown_model_targets_before_lookup(
    monkeypatch,
    app_label,
    model_name,
):
    get_model = mock.Mock(side_effect=AssertionError("model registry was reached"))
    monkeypatch.setattr(preflight.django_apps, "get_model", get_model)

    with pytest.raises(ValueError, match="model target is not approved"):
        preflight._table(app_label, model_name)

    get_model.assert_not_called()


def test_release_preflight_relation_lookup_parameterizes_the_raw_table_name():
    cursor = mock.Mock()
    cursor.fetchone.return_value = (True,)
    reference = preflight._TableReference(
        name='payments_payment"; DROP TABLE payments_payment; --',
        sql='"payments_payment"',
    )

    assert preflight._relation_exists(cursor, reference) is True

    sql, params = cursor.execute.call_args.args
    assert "%s" in sql
    assert reference.name not in sql
    assert params == [reference.name]


@pytest.mark.parametrize("field_name", sorted(preflight._WORKLOAD_FIELD_PREDICATES))
def test_release_preflight_workload_predicates_quote_allowed_columns(field_name):
    column = connection.ops.quote_name(field_name)

    predicate = preflight._workload_predicate(field_name)

    assert column in predicate
    assert field_name not in predicate.replace(column, "")


def test_release_preflight_rejects_unknown_workload_field():
    with pytest.raises(ValueError, match="workload field is not approved"):
        preflight._workload_predicate("notes; DROP TABLE parents_parentprofile; --")


def test_academic_preflight_covers_nonfinite_and_out_of_range_legacy_evidence(monkeypatch):
    monkeypatch.setattr(preflight, "_relation_exists", lambda cursor, table: True)
    scalar = mock.Mock(return_value=0)
    monkeypatch.setattr(preflight, "_scalar", scalar)
    issues: dict[str, int] = {}
    estimates: dict[str, int] = {}

    preflight._academic_checks(mock.Mock(), set(), issues, estimates)

    assert issues["academic_invalid_exam_numeric_rows"] == 0
    assert issues["academic_invalid_result_score_rows"] == 0
    rendered_sql = "\n".join(call.args[1] for call in scalar.call_args_list)
    assert "'NaN', 'Infinity', '-Infinity'" in rendered_sql
    assert "result.score > exam.max_score" in rendered_sql


def test_release_preflight_counts_legacy_parent_scope_reviews(monkeypatch):
    monkeypatch.setattr(preflight, "_relation_exists", lambda cursor, table: True)
    monkeypatch.setattr(preflight, "_scalar", lambda cursor, sql, params=(): 7)
    estimates: dict[str, int] = {}

    preflight._workload_estimates(mock.Mock(), set(), estimates)

    assert estimates["legacy_parent_profiles_requiring_scope_review"] == 7
    assert estimates["legacy_audit_actors_marked_unresolved"] == 7


def test_money_unit_preflight_counts_each_pending_unit_mismatch(monkeypatch):
    monkeypatch.setattr(preflight, "_relation_exists", lambda cursor, table: True)
    scalar = mock.Mock(side_effect=(2, 3))
    monkeypatch.setattr(preflight, "_scalar", scalar)
    issues: dict[str, int] = {}

    preflight._money_unit_checks(mock.Mock(), set(), issues)

    assert issues == {
        "finance_invoice_non_uzs_currency_rows": 2,
        "payments_non_uzs_currency_rows": 3,
    }
    assert all(call.args[2] == ("UZS",) for call in scalar.call_args_list)
    assert all("IS DISTINCT FROM %s" in call.args[1] for call in scalar.call_args_list)


def test_money_unit_preflight_skips_constraints_already_applied(monkeypatch):
    relation_exists = mock.Mock(side_effect=AssertionError("database was inspected"))
    monkeypatch.setattr(preflight, "_relation_exists", relation_exists)
    issues: dict[str, int] = {}
    applied = {
        ("finance", "0010_invoice_currency_uzs"),
        ("payments", "0009_payment_currency_uzs"),
    }

    preflight._money_unit_checks(mock.Mock(), applied, issues)

    assert issues == {}
    relation_exists.assert_not_called()
