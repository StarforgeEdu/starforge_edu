"""Shared row-level scoping for the parent domain (TD-5).

Staff (director / head-of-dept / registrar / IT) see every row; a parent sees
only their own rows (via ``own_filter``); anyone else sees nothing. Mirrors the
old ``apps.parents.selectors`` scoping, kept in one place so all three repos
apply it identically.
"""

from __future__ import annotations

from django.db.models import QuerySet

from core.permissions import Role

STAFF_ROLES = frozenset({Role.DIRECTOR, Role.HEAD_OF_DEPT, Role.REGISTRAR, Role.IT})


def scope_rows(qs: QuerySet, *, user, roles, own_filter: dict) -> QuerySet:
    if getattr(user, "is_superuser", False):
        return qs
    role_set = set(roles or ())
    if role_set & STAFF_ROLES:
        return qs
    if Role.PARENT in role_set:
        return qs.filter(**own_filter)
    return qs.none()
