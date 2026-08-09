"""Data-migration coverage for legacy primary/role teacher relationships."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django_tenants.utils import schema_context

from apps.cohorts.models import CohortTeacher
from apps.cohorts.tests.factories import CohortFactory
from apps.org.tests.factories import BranchFactory
from apps.teachers.models import TeacherType
from apps.teachers.tests.factories import TeacherProfileFactory
from tests.migration_isolation import IsolatedMigrationHarness

TYPED_TARGET = ("cohorts", "0004_typed_teacher_assignments")
FINAL_TARGET = ("cohorts", "0005_finalize_typed_teacher_assignments")


@pytest.mark.django_db(transaction=True)
def test_migration_preserves_primary_and_legacy_role_rows(tenant_a):
    """An old primary teacher who was also a co-teacher retains both responsibilities."""
    migrations = IsolatedMigrationHarness(connection, (TYPED_TARGET, FINAL_TARGET))
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        primary = TeacherProfileFactory(branch=branch)
        assistant = TeacherProfileFactory(branch=branch)
        cohort = CohortFactory(branch=branch, primary_teacher=primary)
        CohortTeacher.objects.create(
            cohort=cohort,
            teacher=primary,
            teacher_type=TeacherType.objects.get(slug="co-teacher"),
        )
        CohortTeacher.objects.create(
            cohort=cohort,
            teacher=assistant,
            teacher_type=TeacherType.objects.get(slug="assistant"),
        )

        try:
            migrations.downgrade()
            executor = MigrationExecutor(connection)
            old_state = executor.loader.project_state([("cohorts", "0003_initial")])
            old_assignment = old_state.apps.get_model("cohorts", "CohortTeacher")
            old_rows = set(
                old_assignment.objects.filter(cohort_id=cohort.id).values_list("teacher_id", "role")
            )
            assert old_rows == {
                (primary.id, "co_teacher"),
                (assistant.id, "assistant"),
            }

            migrations.upgrade()
        finally:
            # Always restore the tenant schema for fixture teardown and later tests.
            migrations.upgrade()

        migrated = set(
            CohortTeacher.objects.filter(cohort_id=cohort.id).values_list(
                "teacher_id", "teacher_type__slug", "role"
            )
        )
        assert migrated == {
            (primary.id, "main-teacher", "co_teacher"),
            (primary.id, "co-teacher", "co_teacher"),
            (assistant.id, "assistant", "assistant"),
        }
