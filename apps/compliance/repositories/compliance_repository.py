"""ORM-backed compliance repositories (rules + penalties with subject/branch scoping)."""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet

from apps.compliance.interfaces.repositories import IPenaltyRepository, IRuleRepository
from apps.compliance.models import Penalty, Rule
from apps.org.models import Branch
from apps.students.models import StudentProfile
from apps.users.models import User
from core.repositories import BaseRepository


class RuleRepository(BaseRepository[Rule], IRuleRepository):
    model = Rule

    def queryset(self) -> QuerySet[Rule]:
        return Rule.objects.all()

    def get(self, *, pk: int) -> Rule | None:
        return Rule.objects.filter(pk=pk).first()

    def get_active(self, *, pk: int) -> Rule | None:
        return Rule.objects.filter(pk=pk, is_active=True).first()

    def add(self, *, data: dict[str, Any]) -> Rule:
        return Rule.objects.create(**data)


class PenaltyRepository(BaseRepository[Penalty], IPenaltyRepository):
    model = Penalty

    def _base(self) -> QuerySet[Penalty]:
        return Penalty.objects.select_related(
            "rule", "student", "student__user", "staff", "branch", "issued_by", "waived_by"
        )

    def scoped(
        self, *, is_director: bool, user, branch_ids: set[int], can_waive: bool, can_write: bool
    ) -> QuerySet[Penalty]:
        qs = self._base()
        if is_director:
            return qs
        # The SUBJECT always sees their own record: a student their demerits (+ a parent
        # their children's), a staff member their own discipline.
        scope = Q(student__user=user) | Q(student__guardians__parent__user=user) | Q(staff=user)
        if can_waive:
            # A manager (waive-capable) handles ALL their branch's penalties — student
            # demerits AND staff discipline.
            scope |= Q(branch_id__in=branch_ids)
        elif can_write:
            # A non-manager issuer (teacher) sees their branch's STUDENT demerits only —
            # staff disciplinary records are NOT visible to peers (HR privacy).
            scope |= Q(branch_id__in=branch_ids, staff__isnull=True)
        return qs.filter(scope).distinct()

    def get_scoped(
        self, *, is_director: bool, user, branch_ids: set[int], can_waive: bool, can_write: bool, pk: int
    ) -> Penalty | None:
        return (
            self.scoped(
                is_director=is_director,
                user=user,
                branch_ids=branch_ids,
                can_waive=can_waive,
                can_write=can_write,
            )
            .filter(pk=pk)
            .first()
        )

    def get_student(self, *, student_id: int) -> StudentProfile | None:
        return StudentProfile.objects.select_related("branch").filter(pk=student_id).first()

    def get_active_rule(self, *, rule_id: int) -> Rule | None:
        return Rule.objects.filter(pk=rule_id, is_active=True).first()

    def get_active_user(self, *, user_id: int) -> User | None:
        return User.objects.filter(pk=user_id, is_active=True).first()

    def get_branch(self, *, branch_id: int) -> Branch | None:
        return Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
