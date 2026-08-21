"""ORM-backed forms repository (role-scoped reads)."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.forms.interfaces.repositories import IFormRepository
from apps.forms.models import Form
from core.repositories import BaseRepository


class FormRepository(BaseRepository[Form], IFormRepository):
    model = Form

    def get_queryset(self) -> QuerySet[Form]:
        return Form.objects.select_related(
            "branch",
            "created_by",
            "created_by__staff_profile",
            "created_by__teacher_profile",
        ).prefetch_related("fields")

    def scoped(
        self,
        *,
        user,
        read_unscoped: bool,
        write_unscoped: bool,
        can_write: bool,
        roles: set[str],
        principal_kind: str,
        principal_id: int,
        read_branch_ids: set[int],
        write_branch_ids: set[int],
    ) -> QuerySet[Form]:
        qs = self.get_queryset()
        manageable = Q(pk__in=[])
        if can_write:
            manageable = Q(pk__isnull=False) if write_unscoped else Q(branch_id__in=write_branch_ids)

        audience = Q(audience_roles=[], audience_user_ids=[], audience_principals=[])
        for role in roles:
            audience |= Q(audience_roles__contains=[role])
        if principal_kind and principal_id > 0:
            audience |= Q(
                audience_principals__contains=[
                    {"kind": principal_kind, "id": principal_id, "user_id": user.pk}
                ]
            )

        readable_branches = Q(pk__isnull=False) if read_unscoped else Q(branch_id__in=read_branch_ids)
        respondable = (
            Q(status=Form.Status.PUBLISHED) & (readable_branches | Q(branch__isnull=True)) & audience
        )
        return qs.filter(manageable | respondable).distinct()

    def get_scoped(
        self,
        *,
        user,
        read_unscoped: bool,
        write_unscoped: bool,
        can_write: bool,
        roles: set[str],
        principal_kind: str,
        principal_id: int,
        read_branch_ids: set[int],
        write_branch_ids: set[int],
        pk: int,
    ) -> Form | None:
        return (
            self.scoped(
                user=user,
                read_unscoped=read_unscoped,
                write_unscoped=write_unscoped,
                can_write=can_write,
                roles=roles,
                principal_kind=principal_kind,
                principal_id=principal_id,
                read_branch_ids=read_branch_ids,
                write_branch_ids=write_branch_ids,
            )
            .filter(pk=pk)
            .first()
        )
