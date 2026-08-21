"""Repository ports (abstract data-access contracts) for the layered architecture.

Services depend on these interfaces, never on the ORM directly — so the business
logic is testable against a fake and the persistence layer is swappable. The
concrete ORM implementation lives in ``core.repositories.BaseRepository``; an
app's repository subclasses it and is bound to its port in ``core.bootstrap``.

Tenant scope is implicit: every query runs inside the active django-tenants
schema (set by middleware), so a repository never has to filter by tenant.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db import models
from django.db.models import QuerySet


class IBaseRepository[T: models.Model](ABC):
    """The CRUD/query contract every repository honours."""

    @abstractmethod
    def get_queryset(self) -> QuerySet[T]:
        """Base queryset — override to bake in ``select_related``/``prefetch_related``
        so callers never trip an N+1."""

    @abstractmethod
    def get_by_id(self, pk: Any) -> T | None: ...

    @abstractmethod
    def filter_by(self, **kwargs: Any) -> QuerySet[T]: ...

    @abstractmethod
    def first(self, **kwargs: Any) -> T | None: ...

    @abstractmethod
    def all(self) -> QuerySet[T]: ...

    @abstractmethod
    def create(self, **kwargs: Any) -> T: ...

    @abstractmethod
    def bulk_create(self, objects: list[T]) -> list[T]: ...

    @abstractmethod
    def update(self, instance: T, **kwargs: Any) -> T: ...

    @abstractmethod
    def delete(self, instance: T) -> None: ...

    @abstractmethod
    def exists(self, **kwargs: Any) -> bool: ...

    @abstractmethod
    def count(self, **kwargs: Any) -> int: ...

    @abstractmethod
    def get_or_create(self, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[T, bool]: ...
