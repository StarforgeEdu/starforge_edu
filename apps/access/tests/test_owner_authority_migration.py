from __future__ import annotations

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django_tenants.utils import schema_context

from apps.access.models import AccountType, AccountTypePermission
from core.permissions import Role

pytestmark = pytest.mark.django_db(transaction=True)

HARDENING_TARGET = ("access", "0005_protect_owner_authority")
LEGACY_TARGET = ("access", "0004_compensation_permissions")
CURRENT_TARGET = ("access", "0006_head_of_department_org_read")
CUSTOM_SLUG = "owner-authority-migration-probe"
OVERRIDE_NOTE = "owner-authority-migration-probe"


def _historical_models(target):
    executor = MigrationExecutor(connection)
    state = executor.loader.project_state([target])
    return (
        state.apps.get_model("access", "AccountType"),
        state.apps.get_model("access", "AccountTypePermission"),
        state.apps.get_model("access", "RolePermissionOverride"),
    )


def _migrate_current_graph() -> None:
    """Restore every leaf that an access downgrade may have unapplied."""

    executor = MigrationExecutor(connection)
    assert CURRENT_TARGET in executor.loader.graph.nodes
    executor.migrate(executor.loader.graph.leaf_nodes())


def test_owner_authority_migration_preflights_applies_and_reverses_cleanly(tenant_a):
    custom_id: int | None = None
    owner_id: int | None = None
    owner_description = ""

    try:
        with schema_context(tenant_a.schema_name):
            try:
                MigrationExecutor(connection).migrate([LEGACY_TARGET])
                LegacyType, LegacyGrant, LegacyOverride = _historical_models(LEGACY_TARGET)
                owner = LegacyType.objects.get(is_system=True, slug=Role.DIRECTOR)
                owner_id = owner.pk
                owner_description = owner.description
                custom = LegacyType.objects.create(
                    name="Owner authority migration probe",
                    slug=CUSTOM_SLUG,
                    account_kind="staff",
                )
                custom_id = custom.pk

                # The deployment must stop before installing constraints if old
                # data has already delegated owner-only authority.
                LegacyGrant.objects.bulk_create(
                    [LegacyGrant(account_type_id=custom_id, permission="access:write")]
                )
                LegacyOverride.objects.create(
                    role=Role.SUPPORT,
                    permission="access:read",
                    effect="grant",
                    note=OVERRIDE_NOTE,
                )
                with pytest.raises(RuntimeError, match="owner-authority preflight failed"):
                    MigrationExecutor(connection).migrate([HARDENING_TARGET])

                LegacyGrant.objects.filter(
                    account_type_id=custom_id,
                    permission="access:write",
                ).delete()
                LegacyOverride.objects.filter(note=OVERRIDE_NOTE).delete()
                MigrationExecutor(connection).migrate([HARDENING_TARGET])

                # Normal custom grants and edits remain available after hardening.
                current_custom = AccountType.objects.get(pk=custom_id)
                AccountTypePermission.objects.create(
                    account_type=current_custom,
                    permission="students:read",
                )
                current_custom.description = "Legitimate custom account type"
                current_custom.save(update_fields=("description", "updated_at"))

                # Raw/bulk writes cannot bypass the database boundary, and the
                # canonical owner row and wildcard remain immutable.
                with pytest.raises(IntegrityError), transaction.atomic():
                    AccountTypePermission.objects.bulk_create(
                        [
                            AccountTypePermission(
                                account_type=current_custom,
                                permission="access:write",
                            )
                        ]
                    )
                with pytest.raises(IntegrityError), transaction.atomic():
                    AccountType.objects.filter(pk=owner_id).update(is_active=False)
                with pytest.raises(IntegrityError), transaction.atomic():
                    AccountTypePermission.objects.filter(
                        account_type_id=owner_id,
                        permission="*:*",
                    ).delete()

                assert AccountType.objects.get(pk=owner_id).is_active is True
                assert AccountTypePermission.objects.filter(
                    account_type_id=owner_id,
                    permission="*:*",
                ).exists()

                # Reversal removes both the trigger and check constraint. Prove
                # the old schema accepts the exact writes, then clean the probe.
                MigrationExecutor(connection).migrate([LEGACY_TARGET])
                LegacyType, LegacyGrant, LegacyOverride = _historical_models(LEGACY_TARGET)
                assert LegacyType.objects.filter(pk=owner_id).update(description="Reverse probe") == 1
                LegacyGrant.objects.create(account_type_id=custom_id, permission="access:write")
                LegacyOverride.objects.create(
                    role=Role.SUPPORT,
                    permission="access:read",
                    effect="grant",
                    note=OVERRIDE_NOTE,
                )
                assert LegacyGrant.objects.filter(
                    account_type_id=custom_id,
                    permission="access:write",
                ).exists()
                assert LegacyOverride.objects.filter(note=OVERRIDE_NOTE).exists()
            finally:
                # Always remove probe data under the legacy graph and restore the
                # current access leaf before leaving this tenant schema.
                MigrationExecutor(connection).migrate([LEGACY_TARGET])
                LegacyType, LegacyGrant, LegacyOverride = _historical_models(LEGACY_TARGET)
                LegacyOverride.objects.filter(note=OVERRIDE_NOTE).delete()
                if custom_id is not None:
                    LegacyGrant.objects.filter(account_type_id=custom_id).delete()
                    LegacyType.objects.filter(pk=custom_id).delete()
                if owner_id is not None:
                    LegacyType.objects.filter(pk=owner_id).update(
                        description=owner_description,
                        is_active=True,
                    )
                _migrate_current_graph()
    finally:
        # TransactionTestCase flushes whichever schema remains selected during
        # teardown. Never let MigrationExecutor leave a tenant selected.
        connection.set_schema_to_public()
