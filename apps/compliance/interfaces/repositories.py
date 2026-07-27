"""Compliance-domain repository ports."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.compliance.models import Penalty, Rule
from apps.org.models import Branch
from apps.students.models import StudentProfile
from apps.users.models import User
from core.interfaces import IBaseRepository


class IRuleRepository(IBaseRepository[Rule]):
    def queryset(self) -> QuerySet[Rule]:
        raise NotImplementedError

    def get(self, *, pk: int) -> Rule | None:
        raise NotImplementedError

    def get_active(self, *, pk: int) -> Rule | None:
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]) -> Rule:
        raise NotImplementedError


class IPenaltyRepository(IBaseRepository[Penalty]):
    def scoped(
        self, *, is_director: bool, user, branch_ids: set[int], can_waive: bool, can_write: bool
    ) -> QuerySet[Penalty]:
        raise NotImplementedError

    def get_scoped(
        self, *, is_director: bool, user, branch_ids: set[int], can_waive: bool, can_write: bool, pk: int
    ) -> Penalty | None:
        raise NotImplementedError

    def get_student(self, *, student_id: int) -> StudentProfile | None:
        raise NotImplementedError

    def get_active_rule(self, *, rule_id: int) -> Rule | None:
        raise NotImplementedError

    def get_active_user(self, *, user_id: int) -> User | None:
        raise NotImplementedError

    def get_branch(self, *, branch_id: int) -> Branch | None:
        raise NotImplementedError
