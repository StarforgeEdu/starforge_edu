"""The shadow-column migrations encrypt legacy safeguarding plaintext safely."""

from __future__ import annotations

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django_tenants.utils import schema_context

from apps.org.models import Branch
from apps.org.tests.factories import BranchFactory
from apps.parents.models import Guardian, ParentProfile
from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
from apps.students.models import StudentProfile
from apps.students.tests.factories import StudentProfileFactory
from tests.migration_isolation import IsolatedMigrationHarness

PARENT_TARGET = ("parents", "0010_preserve_family_lifecycle_history")
PARENT_LEGACY_TARGET = ("parents", "0008_parent_creation_scope")
STUDENT_TARGET = ("students", "0011_protect_identity_history")
STUDENT_LEGACY_TARGET = (
    "students",
    "0009_studentprofile_student_phone_unique_nonblank_and_more",
)
SAFEGUARDING_MIGRATIONS = (
    ("parents", "0009_encrypt_safeguarding_text"),
    ("students", "0010_encrypt_emergency_contacts"),
    STUDENT_TARGET,
    PARENT_TARGET,
)


@pytest.mark.django_db(transaction=True)
def test_shadow_migrations_transform_plaintext_before_cutover(tenant_a):
    parent_secret = "Legacy restricted family note"
    custody_secret = "Legacy restricted court-order note"
    contacts = [{"name": "Legacy guardian", "phone": "+998901234567"}]

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = StudentProfileFactory(branch=branch)
        parent = ParentProfileFactory()
        guardian = GuardianFactory(parent=parent, student=student)
        student_user_id = student.user_id
        parent_user_id = parent.user_id

        migrations = IsolatedMigrationHarness(connection, SAFEGUARDING_MIGRATIONS)
        try:
            # Recreate the deployed legacy column types, then write values through
            # the historical plaintext fields. Current fail-closed field classes
            # must never be used while the legacy schema is active.
            migrations.downgrade()
            executor = MigrationExecutor(connection)
            legacy_state = executor.loader.project_state([PARENT_LEGACY_TARGET, STUDENT_LEGACY_TARGET])
            LegacyParent = legacy_state.apps.get_model("parents", "ParentProfile")
            LegacyGuardian = legacy_state.apps.get_model("parents", "Guardian")
            LegacyStudent = legacy_state.apps.get_model("students", "StudentProfile")
            LegacyParent.objects.filter(pk=parent.pk).update(notes=parent_secret)
            LegacyGuardian.objects.filter(pk=guardian.pk).update(custody_notes=custody_secret)
            LegacyStudent.objects.filter(pk=student.pk).update(emergency_contacts=contacts)

            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT notes FROM {ParentProfile._meta.db_table} WHERE id = %s",  # nosec B608
                    [parent.pk],
                )
                assert cursor.fetchone()[0] == parent_secret
                cursor.execute(
                    f"SELECT custody_notes FROM {Guardian._meta.db_table} WHERE id = %s",  # nosec B608
                    [guardian.pk],
                )
                assert cursor.fetchone()[0] == custody_secret

            # Each forward migration writes an encrypted shadow, authenticates an
            # exact ORM round trip, and only then removes the plaintext column.
            migrations.upgrade()

            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT notes FROM {ParentProfile._meta.db_table} WHERE id = %s",  # nosec B608
                    [parent.pk],
                )
                raw_parent = cursor.fetchone()[0]
                cursor.execute(
                    f"SELECT custody_notes FROM {Guardian._meta.db_table} WHERE id = %s",  # nosec B608
                    [guardian.pk],
                )
                raw_custody = cursor.fetchone()[0]
                cursor.execute(
                    f"SELECT emergency_contacts FROM {StudentProfile._meta.db_table} WHERE id = %s",  # nosec B608
                    [student.pk],
                )
                raw_contacts = cursor.fetchone()[0]

            assert raw_parent.startswith("gAAAA")
            assert raw_custody.startswith("gAAAA")
            assert raw_contacts.startswith("gAAAA")
            assert parent_secret not in raw_parent
            assert custody_secret not in raw_custody
            assert "+998901234567" not in raw_contacts

            assert ParentProfile.objects.get(pk=parent.pk).notes == parent_secret
            assert Guardian.objects.get(pk=guardian.pk).custody_notes == custody_secret
            assert StudentProfile.objects.get(pk=student.pk).emergency_contacts == contacts
        finally:
            # Always restore this exact migration slice so fixture teardown and
            # following tests see the normal model.
            migrations.upgrade()
            # pytest-django's transaction=True flush runs on whichever schema is
            # current at teardown; schema_context restores public first. Remove
            # this tenant fixture explicitly so reused databases and later
            # student-limit tests cannot inherit an active student.
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL starforge.identity_history_maintenance = 'on'")
                    cursor.execute("SET LOCAL starforge.org_history_maintenance = 'on'")
                Guardian.objects.filter(pk=guardian.pk).delete()
                ParentProfile.objects.filter(pk=parent.pk).delete()
                StudentProfile.objects.filter(pk=student.pk).delete()
                from apps.users.models import User

                User.objects.filter(pk__in=(student_user_id, parent_user_id)).delete()
                # Model-level lifecycle protection intentionally rejects instance
                # deletion. Test cleanup uses the same explicit maintenance mode
                # as the database history guards and bypasses only that model hook.
                Branch.objects.filter(pk=branch.pk).delete()
