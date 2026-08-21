"""Compliance services — orchestration over the preserved domain fns
(update_rule_body/acknowledge/issue_penalty/issue_staff_penalty/waive_penalty) + the
role-based rule selectors."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.compliance import selectors
from apps.compliance import services as domain
from apps.compliance.interfaces.repositories import IPenaltyRepository, IRuleRepository
from apps.compliance.interfaces.services import IPenaltyService, IRuleService
from apps.compliance.models import Penalty, Rule, RuleAcknowledgment
from apps.org.models import Branch
from apps.students.models import StudentProfile
from apps.users.models import User


class RuleService(IRuleService):
    def __init__(self, repository: IRuleRepository) -> None:
        self.repository = repository

    def list_rules(self) -> QuerySet[Rule]:
        return self.repository.queryset()

    def get(self, *, pk: int) -> Rule | None:
        return self.repository.get(pk=pk)

    def get_active(self, *, pk: int) -> Rule | None:
        return self.repository.get_active(pk=pk)

    def create(self, *, title: str, body: str, applies_to_roles: list, is_active: bool, created_by) -> Rule:
        return self.repository.add(
            data={
                "title": title,
                "body": body,
                "applies_to_roles": applies_to_roles,
                "is_active": is_active,
                "created_by": created_by,
            }
        )

    def update(self, rule: Rule, changes: dict[str, Any]) -> Rule:
        # update_rule_body bumps the version only when the body actually changes.
        body = changes.pop("body", None)
        title = changes.pop("title", None)
        return domain.update_rule_body(rule=rule, body=body, title=title, **changes)

    def delete(self, rule: Rule) -> None:
        # DELETE is a semantic retirement. Keeping the row preserves the exact title,
        # body and version referenced by historical acknowledgments and penalties.
        if rule.is_active:
            rule.is_active = False
            rule.save(update_fields=["is_active", "updated_at"])

    def mine(self, *, user, roles) -> tuple[list[Rule], set[int]]:
        rules = selectors.rules_for_roles(roles)
        return rules, selectors.acknowledged_rule_ids_current(user, rules)

    def pending(self, *, user, roles) -> list[Rule]:
        return selectors.pending_rules(user, roles)

    def acknowledge(self, *, rule: Rule, user) -> RuleAcknowledgment:
        return domain.acknowledge(rule=rule, user=user)


class PenaltyService(IPenaltyService):
    def __init__(self, repository: IPenaltyRepository) -> None:
        self.repository = repository

    def scoped_list(
        self,
        *,
        is_director: bool,
        user,
        waive_branch_ids: set[int],
        write_branch_ids: set[int],
        can_waive: bool,
        can_write: bool,
    ) -> QuerySet[Penalty]:
        return self.repository.scoped(
            is_director=is_director,
            user=user,
            waive_branch_ids=waive_branch_ids,
            write_branch_ids=write_branch_ids,
            can_waive=can_waive,
            can_write=can_write,
        )

    def get_visible(
        self,
        *,
        is_director: bool,
        user,
        waive_branch_ids: set[int],
        write_branch_ids: set[int],
        can_waive: bool,
        can_write: bool,
        pk: int,
    ) -> Penalty | None:
        return self.repository.get_scoped(
            is_director=is_director,
            user=user,
            waive_branch_ids=waive_branch_ids,
            write_branch_ids=write_branch_ids,
            can_waive=can_waive,
            can_write=can_write,
            pk=pk,
        )

    def resolve_student(self, *, student_id: int) -> StudentProfile | None:
        return self.repository.get_student(student_id=student_id)

    def resolve_active_rule(self, *, rule_id: int) -> Rule | None:
        return self.repository.get_active_rule(rule_id=rule_id)

    def resolve_active_user(self, *, user_id: int) -> User | None:
        return self.repository.get_active_user(user_id=user_id)

    def resolve_branch(self, *, branch_id: int) -> Branch | None:
        return self.repository.get_branch(branch_id=branch_id)

    def issue(self, *, student, points: int, reason: str, issued_by, rule) -> Penalty:
        return domain.issue_penalty(
            student=student, points=points, reason=reason, issued_by=issued_by, rule=rule
        )

    def issue_staff(self, *, staff, branch, points: int, reason: str, issued_by, rule) -> Penalty:
        return domain.issue_staff_penalty(
            staff=staff, branch=branch, points=points, reason=reason, issued_by=issued_by, rule=rule
        )

    def waive(self, penalty: Penalty, *, actor, reason: str) -> Penalty:
        return domain.waive_penalty(penalty_id=penalty.pk, actor=actor, reason=reason)
