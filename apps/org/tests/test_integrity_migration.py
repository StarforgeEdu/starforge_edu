"""Upgrade/reverse coverage for organization history hardening."""

from __future__ import annotations

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django_tenants.utils import schema_context

from tests.migration_isolation import IsolatedMigrationHarness

pytestmark = pytest.mark.django_db(transaction=True)

LEGACY_TARGET = ("org", "0019_centersettings_organization_timezone")
CURRENT_TARGET = ("org", "0021_durable_center_settings")
INTEGRITY_TARGET = ("org", "0020_org_scope_and_history_integrity")
GENERALIZED_TRANSFER_TARGET = ("org", "0024_generalize_branch_transfers")
ORG_MIGRATIONS = (INTEGRITY_TARGET, CURRENT_TARGET, GENERALIZED_TRANSFER_TARGET)


def _legacy_models():
    executor = MigrationExecutor(connection)
    state = executor.loader.project_state([LEGACY_TARGET])
    return (
        state.apps.get_model("org", "BranchTransfer"),
        state.apps.get_model("org", "Department"),
    )


def test_org_integrity_migration_preflights_backfills_and_reverses(tenant_a):
    from apps.org.services import create_staff_account
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.tests.factories import UserFactory
    from core.permissions import Role

    transfer_ids: list[int] = []
    profile_ids: dict[str, int] = {}
    migrations = IsolatedMigrationHarness(connection, ORG_MIGRATIONS)
    try:
        with schema_context(tenant_a.schema_name):
            source = BranchFactory(name="Legacy source", slug="legacy-source")
            target = BranchFactory(name="Legacy target", slug="legacy-target")
            department = DepartmentFactory(branch=source, budget="0.00")
            student = StudentProfileFactory(
                branch=source,
                student_id="LEGACY-TRANSFER-001",
                first_name="Legacy",
                last_name="Student",
            )
            actor = create_staff_account(
                branch=source,
                role=Role.SUPPORT,
                username="legacy-transfer-actor",
                first_name="Exact",
                last_name="Operator",
            )
            unresolved_user = UserFactory(username="unresolved-transfer-user")
            profile_ids = {
                "student": student.pk,
                "student_user": student.user_id,
                "actor": actor.pk,
                "actor_user": actor.user_id,
                "unresolved_user": unresolved_user.pk,
                "department": department.pk,
                "source": source.pk,
                "target": target.pk,
            }

            migrations.downgrade()
            LegacyTransfer, LegacyDepartment = _legacy_models()
            exact = LegacyTransfer.objects.create(
                user_id=student.user_id,
                from_branch_id=source.pk,
                to_branch_id=target.pk,
                reason="exact history",
                actor_id=actor.user_id,
            )
            unresolved = LegacyTransfer.objects.create(
                user_id=unresolved_user.pk,
                from_branch_id=source.pk,
                to_branch_id=target.pk,
                reason="unresolved history",
                actor_id=unresolved_user.pk,
            )
            transfer_ids = [exact.pk, unresolved.pk]

            # Existing invalid money data must stop deployment before the check
            # constraint is installed; the migration never guesses a repair.
            LegacyDepartment.objects.filter(pk=department.pk).update(budget="-1.00")
            with pytest.raises(RuntimeError, match="organization integrity preflight failed"):
                migrations.upgrade()

            LegacyDepartment.objects.filter(pk=department.pk).update(budget="0.00")
            migrations.upgrade()

            from apps.org.models import BranchTransfer, CenterSettings, Department

            exact_row = BranchTransfer.objects.get(pk=exact.pk)
            assert exact_row.student_id == student.pk
            assert exact_row.student_public_id == "LEGACY-TRANSFER-001"
            assert exact_row.student_name == "Legacy Student"
            assert exact_row.student_attribution_status == "resolved"
            assert exact_row.actor_principal_kind == "staff"
            assert exact_row.actor_principal_id == actor.pk
            assert exact_row.actor_name == "Exact Operator"

            unresolved_row = BranchTransfer.objects.get(pk=unresolved.pk)
            assert unresolved_row.student_id is None
            assert unresolved_row.student_public_id == ""
            assert unresolved_row.student_name == ""
            assert unresolved_row.student_attribution_status == "unresolved"
            assert unresolved_row.actor_principal_kind == ""
            assert unresolved_row.actor_principal_id is None
            assert unresolved_row.actor_name == ""

            with pytest.raises(IntegrityError), transaction.atomic():
                Department.objects.filter(pk=department.pk).update(budget="-1.00")
            settings_row = CenterSettings.objects.get(pk=1)
            assert settings_row.disabled_apps == []
            with pytest.raises(IntegrityError), transaction.atomic():
                CenterSettings.objects.filter(pk=1).update(disabled_apps=["placement", "placement"])
            with pytest.raises(IntegrityError), transaction.atomic():
                CenterSettings.objects.filter(pk=1).update(currency_primary="usd")
            with pytest.raises(DatabaseError), transaction.atomic():
                BranchTransfer.objects.filter(pk=exact.pk).update(reason="forged history")

            # Reverse removes the new checks and triggers without destroying
            # pre-existing columns or records. Clean the probes before restoring
            # the current graph so the forward preflight succeeds again.
            migrations.downgrade()
            LegacyTransfer, LegacyDepartment = _legacy_models()
            assert LegacyTransfer.objects.filter(pk=exact.pk).update(reason="reverse probe") == 1
            assert LegacyDepartment.objects.filter(pk=department.pk).update(budget="-1.00") == 1
            LegacyDepartment.objects.filter(pk=department.pk).update(budget="0.00")
            migrations.upgrade()
    finally:
        try:
            with schema_context(tenant_a.schema_name):
                # Always restore the leaf before cleanup, even when a forward
                # assertion failed and left the tenant at the legacy graph.
                migrations.downgrade()
                LegacyTransfer, LegacyDepartment = _legacy_models()
                if transfer_ids:
                    LegacyTransfer.objects.filter(pk__in=transfer_ids).delete()
                if profile_ids:
                    LegacyDepartment.objects.filter(pk=profile_ids["department"]).delete()
                migrations.upgrade()

                from apps.org.models import Branch, StaffProfile
                from apps.students.models import StudentProfile
                from apps.users.models import User

                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL starforge.identity_history_maintenance = 'on'")
                        cursor.execute("SET LOCAL starforge.org_history_maintenance = 'on'")
                    if profile_ids:
                        StudentProfile.objects.filter(pk=profile_ids["student"]).delete()
                        StaffProfile.objects.filter(pk=profile_ids["actor"]).delete()
                        User.objects.filter(
                            pk__in=(
                                profile_ids["student_user"],
                                profile_ids["actor_user"],
                                profile_ids["unresolved_user"],
                            )
                        ).delete()
                        Branch.objects.filter(pk__in=(profile_ids["source"], profile_ids["target"])).delete()
        finally:
            # TransactionTestCase flushes the selected schema at teardown.
            connection.set_schema_to_public()
