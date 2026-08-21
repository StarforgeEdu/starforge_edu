"""Cover-request repository port.

Read scoping is role-based: a director/superuser sees the whole centre; a manager
(cover:approve) sees their branch's requests; a teacher sees their own requests, the
claimable pool in their branch, and requests assigned to them.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.covers.models import CoverRequest
from core.interfaces import IBaseRepository


class ICoverRepository(IBaseRepository[CoverRequest]):
    def scoped(
        self,
        *,
        user,
        is_unscoped: bool,
        is_manager: bool,
        manager_branch_ids: set[int],
        teacher_branch_ids: set[int],
    ) -> QuerySet[CoverRequest]:
        raise NotImplementedError

    def get_scoped(
        self,
        *,
        user,
        is_unscoped: bool,
        is_manager: bool,
        manager_branch_ids: set[int],
        teacher_branch_ids: set[int],
        pk: int,
    ) -> CoverRequest | None:
        raise NotImplementedError
