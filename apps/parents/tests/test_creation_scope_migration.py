"""Migration regressions for fail-closed legacy parent ownership."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db(transaction=True)

LEGACY_TARGET = ("parents", "0007_parentprofile_parent_phone_unique_nonblank_and_more")
CREATION_SCOPE_TARGET = ("parents", "0008_parent_creation_scope")


def _restore_current_graph() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def test_current_student_placement_is_not_treated_as_parent_creation_history(tenant_a):
    """A mutable guardian/student relation cannot resolve an immutable snapshot."""

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

            executor = MigrationExecutor(connection)
            executor.migrate([LEGACY_TARGET])
            executor = MigrationExecutor(connection)
            executor.migrate([CREATION_SCOPE_TARGET])
            migrated_state = executor.loader.project_state([CREATION_SCOPE_TARGET])
            MigratedParent = migrated_state.apps.get_model("parents", "ParentProfile")
            migrated = MigratedParent.objects.get(pk=parent_id)

            assert migrated.attribution_status == "unresolved"
            assert migrated.branch_at_creation_id is None
            assert migrated.department_at_creation_id is None
            assert migrated.created_by_id is None
    finally:
        with schema_context(tenant_a.schema_name):
            _restore_current_graph()
        connection.set_schema_to_public()
