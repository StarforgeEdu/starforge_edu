"""Safeguarding cutover detection is schema-safe and strictly read-only."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test.utils import CaptureQueriesContext
from django_tenants.utils import get_public_schema_name, schema_context

from apps.tenancy.management.commands.check_safeguarding_encryption_cutover import (
    REQUIRED_MIGRATIONS,
    pending_tenant_schema_count,
    requires_safeguarding_cutover,
)

pytestmark = pytest.mark.django_db


def test_cutover_requirement_is_all_or_nothing() -> None:
    applied = set(REQUIRED_MIGRATIONS)

    assert ("notifications", "0012_recipient_principal_attribution") in applied
    assert requires_safeguarding_cutover(applied) is False
    assert requires_safeguarding_cutover(applied - {next(iter(applied))}) is True


def test_token_check_ignores_corrupt_business_ciphertext_and_is_read_only(tenant_a) -> None:
    from apps.org.tests.factories import BranchFactory
    from apps.parents.models import Guardian, ParentProfile
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.models import StudentProfile
    from apps.students.tests.factories import StudentProfileFactory

    corrupt_parent = "corrupt-parent-cutover-token"
    corrupt_custody = "corrupt-custody-cutover-token"
    corrupt_contacts = "corrupt-contact-cutover-token"
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(branch=BranchFactory())
        parent = ParentProfileFactory()
        guardian = GuardianFactory(parent=parent, student=student)
        with connection.cursor() as cursor:
            # Model a malformed pre-cutover row through the database's explicit,
            # transaction-local maintenance capability. Production writes keep
            # the history guards enabled, and the bypass is turned off before
            # the command under test runs.
            cursor.execute("SET LOCAL starforge.identity_history_maintenance = 'on'")
            cursor.execute(
                f"UPDATE {ParentProfile._meta.db_table} SET notes = %s WHERE id = %s",  # nosec B608
                [corrupt_parent, parent.pk],
            )
            cursor.execute(
                f"UPDATE {Guardian._meta.db_table} SET custody_notes = %s WHERE id = %s",  # nosec B608
                [corrupt_custody, guardian.pk],
            )
            cursor.execute(
                f"UPDATE {StudentProfile._meta.db_table} SET emergency_contacts = %s WHERE id = %s",  # nosec B608
                [corrupt_contacts, student.pk],
            )
            cursor.execute("SET LOCAL starforge.identity_history_maintenance = 'off'")

    stdout = StringIO()
    with CaptureQueriesContext(connection) as queries:
        call_command(
            "check_safeguarding_encryption_cutover",
            token=True,
            stdout=stdout,
        )

    assert stdout.getvalue().strip() == "clear"
    sql = "\n".join(query["sql"].lower() for query in queries.captured_queries)
    assert ParentProfile._meta.db_table not in sql
    assert Guardian._meta.db_table not in sql
    assert StudentProfile._meta.db_table not in sql
    assert "insert " not in sql
    assert "update " not in sql
    assert "delete " not in sql

    # A check is its own dry run: it must neither authenticate nor rewrite a
    # malformed token. Raw reads avoid invoking the fail-closed model decoder.
    with schema_context(tenant_a.schema_name), connection.cursor() as cursor:
        cursor.execute(
            f"SELECT notes FROM {ParentProfile._meta.db_table} WHERE id = %s",  # nosec B608
            [parent.pk],
        )
        assert cursor.fetchone()[0] == corrupt_parent
        cursor.execute(
            f"SELECT custody_notes FROM {Guardian._meta.db_table} WHERE id = %s",  # nosec B608
            [guardian.pk],
        )
        assert cursor.fetchone()[0] == corrupt_custody
        cursor.execute(
            f"SELECT emergency_contacts FROM {StudentProfile._meta.db_table} WHERE id = %s",  # nosec B608
            [student.pk],
        )
        assert cursor.fetchone()[0] == corrupt_contacts


def test_required_state_is_counted_per_schema_without_identifier_disclosure(
    tenant_a,
    monkeypatch,
) -> None:
    real_applied_migrations = MigrationRecorder.applied_migrations
    missing_migration = ("parents", "0009_encrypt_safeguarding_text")

    def applied_without_one_tenant_cutover(recorder):
        applied = dict(real_applied_migrations(recorder))
        if connection.schema_name == tenant_a.schema_name:
            applied.pop(missing_migration, None)
        return applied

    monkeypatch.setattr(
        MigrationRecorder,
        "applied_migrations",
        applied_without_one_tenant_cutover,
    )

    pending, inspected = pending_tenant_schema_count()
    assert pending == 1
    assert inspected >= 2

    token_output = StringIO()
    # "required" is a valid machine-readable state and deliberately exits
    # successfully; the deployment wrapper parses it and enforces exit 78 when
    # the maintenance acknowledgement is absent.
    call_command(
        "check_safeguarding_encryption_cutover",
        token=True,
        stdout=token_output,
    )
    assert token_output.getvalue().strip() == "required"

    human_output = StringIO()
    call_command(
        "check_safeguarding_encryption_cutover",
        stdout=human_output,
        no_color=True,
    )
    message = human_output.getvalue()
    assert f"1 of {inspected} tenant schemas" in message
    assert tenant_a.schema_name not in message


def test_missing_tenant_migration_table_fails_closed_without_naming_schema(db) -> None:
    from apps.tenancy.models import Center

    public_schema = get_public_schema_name()
    missing_schema = "cutover_missing_schema"
    with schema_context(public_schema):
        center = Center(
            name="Uninspectable cutover fixture",
            slug="uninspectable-cutover-fixture",
            schema_name=missing_schema,
        )
        center.auto_create_schema = False
        center.save()

    with pytest.raises(CommandError) as exc_info:
        pending_tenant_schema_count()

    message = str(exc_info.value)
    assert "without a local migration table" in message
    assert missing_schema not in message
