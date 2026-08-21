"""ORM-backed teacher repository — bakes in select_related so list/detail never N+1."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.access.models import AccountType
from apps.teachers.interfaces.repositories import ITeacherRepository
from apps.teachers.models import TeacherProfile
from apps.users.models import RoleMembership
from core.permissions import Role
from core.repositories import BaseRepository


class TeacherRepository(BaseRepository[TeacherProfile], ITeacherRepository):
    model = TeacherProfile

    def get_queryset(self) -> QuerySet[TeacherProfile]:
        # user/branch/department are read by the presenter on every row — eager-load
        # them so a page of N teachers is 1 query, not 1 + 3N.
        from django.db.models import Prefetch

        return TeacherProfile.objects.select_related("user", "branch", "department").prefetch_related(
            Prefetch(
                "user__role_memberships",
                queryset=(
                    RoleMembership.objects.filter(revoked_at__isnull=True)
                    .filter(
                        Q(
                            account_type__account_kind=AccountType.AccountKind.TEACHER,
                            account_type__is_active=True,
                        )
                        | Q(account_type__isnull=True, role=Role.TEACHER)
                    )
                    .select_related("account_type", "branch", "department")
                ),
            )
        )

    def for_user(self, user) -> TeacherProfile | None:
        return self.get_queryset().filter(user=user).first()
