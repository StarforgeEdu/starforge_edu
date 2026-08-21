"""Helpers for migration tests which must not rewrite unrelated app history."""

from __future__ import annotations

from collections.abc import Sequence

from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.loader import MigrationLoader

MigrationTarget = tuple[str, str]


class IsolatedMigrationHarness:
    """Apply or unapply an exact, topologically ordered migration slice.

    Django's normal ``executor.migrate([old_target])`` un-applies every descendant
    in the project graph. That is appropriate for a real deployment rollback, but
    it makes a focused migration test mutate unrelated apps and can strand the
    shared test schema if a descendant is deliberately irreversible. This harness
    invokes only the operations owned by the test and deliberately leaves the
    migration recorder unchanged.
    """

    def __init__(
        self,
        connection: BaseDatabaseWrapper,
        targets: Sequence[MigrationTarget],
    ) -> None:
        self.connection = connection
        self.targets = tuple(targets)
        self.applied_count = len(self.targets)

    def migrate_to(self, applied_count: int) -> None:
        if not 0 <= applied_count <= len(self.targets):
            raise ValueError("applied_count is outside the isolated migration slice")

        while self.applied_count > applied_count:
            target = self.targets[self.applied_count - 1]
            self._run(target, backwards=True)
            self.applied_count -= 1

        while self.applied_count < applied_count:
            target = self.targets[self.applied_count]
            self._run(target, backwards=False)
            self.applied_count += 1

    def downgrade(self) -> None:
        self.migrate_to(0)

    def upgrade(self) -> None:
        self.migrate_to(len(self.targets))

    def _run(self, target: MigrationTarget, *, backwards: bool) -> None:
        loader = MigrationLoader(self.connection)
        migration = loader.get_migration(*target)
        state_before = loader.project_state([target], at_end=False)
        with self.connection.schema_editor(atomic=migration.atomic) as schema_editor:
            if backwards:
                migration.unapply(state_before, schema_editor)
            else:
                migration.apply(state_before, schema_editor)
