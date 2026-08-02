"""ORM-backed parent repository — bakes in select_related and the role scoping."""

from __future__ import annotations

from django.db.models import Exists, OuterRef, QuerySet

from apps.parents.interfaces.repositories import IParentRepository
from apps.parents.models import Guardian, ParentProfile
from apps.parents.repositories.scoping import (
    attributed_unassigned_parent_scope_q,
    scope_rows,
)
from core.repositories import BaseRepository


class ParentRepository(BaseRepository[ParentProfile], IParentRepository):
    model = ParentProfile

    def get_queryset(self) -> QuerySet[ParentProfile]:
        # Parent identity is role-native; collection presenters do not read the
        # internal authorization bridge or creation-scope relations.
        return ParentProfile.objects.defer("notes")

    def scoped(self, *, user, roles, permission: str) -> QuerySet[ParentProfile]:
        base = self.get_queryset().annotate(
            _has_active_guardian=Exists(
                Guardian.objects.filter(parent_id=OuterRef("pk"), revoked_at__isnull=True)
            )
        )
        linked = scope_rows(
            base.filter(_has_active_guardian=True, guardianships__revoked_at__isnull=True),
            user=user,
            roles=roles,
            permission=permission,
            own_filter={"user": user},
            branch_field="guardianships__student__branch_id",
            department_field="guardianships__student__current_cohort__department_id",
        ).distinct()
        attributed_unassigned = (
            base.filter(_has_active_guardian=False)
            .filter(
                attributed_unassigned_parent_scope_q(
                    user=user,
                    roles=roles,
                    permission=permission,
                )
            )
            .distinct()
        )
        return (linked | attributed_unassigned).distinct()

    def get_scoped(
        self,
        *,
        user,
        roles,
        permission: str,
        pk: int,
    ) -> ParentProfile | None:
        return self.scoped(user=user, roles=roles, permission=permission).filter(pk=pk).first()

    def profile_for(self, user) -> ParentProfile | None:
        return self.get_queryset().filter(user=user).first()

    def students_for(
        self,
        parent,
        *,
        user=None,
        roles=None,
        permission: str = "parents:read",
    ) -> QuerySet:
        # The sanctioned parents->students link (Guardian). select_related the
        # relations the student presenter reads so a family list is not N+1.
        from apps.students.models import StudentProfile

        qs = (
            StudentProfile.objects.filter(
                guardians__parent=parent,
                guardians__revoked_at__isnull=True,
            )
            .select_related("user", "branch", "current_cohort")
            .defer("medical_notes", "emergency_contacts")
            .distinct()
        )
        if user is None:
            return qs
        return scope_rows(
            qs,
            user=user,
            roles=roles,
            permission=permission,
            own_filter={
                "guardians__parent__user": user,
                "guardians__revoked_at__isnull": True,
            },
            branch_field="branch_id",
            department_field="current_cohort__department_id",
        )

    def all_students_in_scope(
        self,
        parent,
        *,
        user,
        roles,
        permission: str,
        lock_parent: bool = True,
    ) -> bool:
        # The caller wraps parent-wide mutations in one transaction. Locking the
        # parent makes a concurrent Guardian FK insert wait. Lock active Guardian
        # rows and their children as well: branch transfers lock StudentProfile,
        # while a relationship revocation locks Guardian. Without all three
        # locks, either operation could move the boundary after authorization but
        # before a parent identity/credential write commits.
        parent_qs = ParentProfile.objects.all()
        if lock_parent:
            parent_qs = parent_qs.select_for_update()
        if not parent_qs.filter(pk=parent.pk).exists():
            return False
        if lock_parent:
            active_student_ids = sorted(
                set(
                    Guardian.objects.select_for_update()
                    .filter(parent_id=parent.pk, revoked_at__isnull=True)
                    .order_by("pk")
                    .values_list("student_id", flat=True)
                )
            )
            if active_student_ids:
                from apps.students.models import StudentProfile

                # Evaluate the queryset so the row locks are actually acquired.
                list(
                    StudentProfile.objects.select_for_update()
                    .filter(pk__in=active_student_ids)
                    .order_by("pk")
                    .values_list("pk", flat=True)
                )
        all_students = self.students_for(parent)
        if not all_students.exists():
            # Before the first Guardian link, the immutable creation snapshot is
            # the only safe scope evidence. Unresolved legacy/owner drafts match
            # only an organization-wide permission.
            return self.scoped(user=user, roles=roles, permission=permission).filter(pk=parent.pk).exists()
        scoped_ids = self.students_for(
            parent,
            user=user,
            roles=roles,
            permission=permission,
        ).values("pk")
        return not all_students.exclude(pk__in=scoped_ids).exists()
