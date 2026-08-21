"""Migration regressions for legacy assessment evidence."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django_tenants.utils import schema_context

from tests.migration_isolation import IsolatedMigrationHarness

pytestmark = pytest.mark.django_db(transaction=True)

LEGACY_TARGET = ("academics", "0003_examtype_remove_exam_type_exam_exam_type")
INTEGRITY_TARGET = ("academics", "0004_assessment_integrity")


def test_legacy_computed_grades_are_invalid_until_recomputed(tenant_a):
    """Adding the integrity fields must not silently certify stale evidence."""

    migrations = IsolatedMigrationHarness(connection, (INTEGRITY_TARGET,))
    try:
        with schema_context(tenant_a.schema_name):
            from apps.academics.tests.factories import GradeFactory

            grade = GradeFactory()
            grade_id = grade.pk

            migrations.downgrade()
            migrations.upgrade()
            executor = MigrationExecutor(connection)
            state = executor.loader.project_state([INTEGRITY_TARGET])
            MigratedGrade = state.apps.get_model("academics", "Grade")
            migrated = MigratedGrade.objects.get(pk=grade_id)

            assert migrated.is_valid is False
            assert migrated.invalidated_at is not None
            assert migrated.invalidation_reason == "legacy_unverified"
    finally:
        try:
            with schema_context(tenant_a.schema_name):
                migrations.upgrade()
        finally:
            connection.set_schema_to_public()
