"""Forms repository port.

Read scoping is role-based: a director sees the whole centre; a builder (forms:write)
sees their own forms + their branch(es)'; a responder (forms:read) sees only PUBLISHED
forms in their branch or centre-wide (branch null).
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.forms.models import Form
from core.interfaces import IBaseRepository


class IFormRepository(IBaseRepository[Form]):
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
        raise NotImplementedError

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
        raise NotImplementedError
