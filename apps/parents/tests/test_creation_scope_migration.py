"""Migration regressions for fail-closed legacy parent ownership."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django_tenants.utils import schema_context

from tests.migration_isolation import IsolatedMigrationHarness

pytestmark = pytest.mark.django_db(transaction=True)

CREATION_SCOPE_TARGET = ("parents", "0008_parent_creation_scope")
PARENT_MIGRATIONS = (
    CREATION_SCOPE_TARGET,
    ("parents", "0009_encrypt_safeguarding_text"),
    ("parents", "0010_preserve_family_lifecycle_history"),
)


def test_current_student_placement_is_not_treated_as_parent_creation_history(tenant_a):
    """A mutable guardian/student relation cannot resolve an immutable snapshot."""

    migrations = IsolatedMigrationHarness(connection, PARENT_MIGRATIONS)
    try:
        with schema_context(tenant_a.schema_name):
            from apps.org.tests.factories import BranchFactory
            from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
            from apps.students.tests.factories import StudentProfileFactory

            current_branch = BranchFactory()
            student = StudentProfileFactory(branch=current_branch)
            parent = ParentProfileFactory()
            GuardianFactory(parent=parent, student=student)
            parent_id = parent.pk

            migrations.downgrade()
            migrations.migrate_to(1)
            executor = MigrationExecutor(connection)
            migrated_state = executor.loader.project_state([CREATION_SCOPE_TARGET])
            MigratedParent = migrated_state.apps.get_model("parents", "ParentProfile")
            migrated = MigratedParent.objects.get(pk=parent_id)

            assert migrated.attribution_status == "unresolved"
            assert migrated.branch_at_creation_id is None
            assert migrated.department_at_creation_id is None
            assert migrated.created_by_id is None
    finally:
        try:
            with schema_context(tenant_a.schema_name):
                migrations.upgrade()
        finally:
            connection.set_schema_to_public()
