"""Static release guards for cross-app migration dependencies."""

from __future__ import annotations

from django.db.migrations.loader import MigrationLoader


def test_notification_principal_trigger_follows_role_identity_columns():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    target = ("notifications", "0012_recipient_principal_attribution")
    ancestors = set(loader.graph.forwards_plan(target))

    assert ("org", "0021_durable_center_settings") in ancestors
    assert ("parents", "0010_preserve_family_lifecycle_history") in ancestors
    assert ("students", "0011_protect_identity_history") in ancestors
    assert ("teachers", "0010_alter_payoutpolicy_method") in ancestors


def test_release_migration_graph_has_one_leaf_per_app():
    loader = MigrationLoader(None, ignore_no_migrations=True)

    assert loader.detect_conflicts() == {}
