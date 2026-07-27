"""Compliance-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.compliance.models import Penalty, Rule, RuleAcknowledgment
from apps.org.models import Branch
from apps.students.models import StudentProfile
from apps.users.models import User


class IRuleService(ABC):
    @abstractmethod
    def list_rules(self) -> QuerySet[Rule]: ...

    @abstractmethod
    def get(self, *, pk: int) -> Rule | None: ...

    @abstractmethod
    def get_active(self, *, pk: int) -> Rule | None: ...

    @abstractmethod
    def create(
        self, *, title: str, body: str, applies_to_roles: list, is_active: bool, created_by
    ) -> Rule: ...

    @abstractmethod
    def update(self, rule: Rule, changes: dict[str, Any]) -> Rule: ...

    @abstractmethod
    def delete(self, rule: Rule) -> None: ...

    @abstractmethod
    def mine(self, *, user, roles) -> tuple[list[Rule], set[int]]: ...

    @abstractmethod
    def pending(self, *, user, roles) -> list[Rule]: ...

    @abstractmethod
    def acknowledge(self, *, rule: Rule, user) -> RuleAcknowledgment: ...


class IPenaltyService(ABC):
    @abstractmethod
    def scoped_list(
        self, *, is_director: bool, user, branch_ids: set[int], can_waive: bool, can_write: bool
    ) -> QuerySet[Penalty]: ...

    @abstractmethod
    def get_visible(
        self, *, is_director: bool, user, branch_ids: set[int], can_waive: bool, can_write: bool, pk: int
    ) -> Penalty | None: ...

    @abstractmethod
    def resolve_student(self, *, student_id: int) -> StudentProfile | None: ...

    @abstractmethod
    def resolve_active_rule(self, *, rule_id: int) -> Rule | None: ...

    @abstractmethod
    def resolve_active_user(self, *, user_id: int) -> User | None: ...

    @abstractmethod
    def resolve_branch(self, *, branch_id: int) -> Branch | None: ...

    @abstractmethod
    def issue(self, *, student, points: int, reason: str, issued_by, rule) -> Penalty: ...

    @abstractmethod
    def issue_staff(self, *, staff, branch, points: int, reason: str, issued_by, rule) -> Penalty: ...

    @abstractmethod
    def waive(self, penalty: Penalty, *, actor, reason: str) -> Penalty: ...
