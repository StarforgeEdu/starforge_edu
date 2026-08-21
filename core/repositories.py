"""ORM-backed base repository — the only layer that touches the Django ORM.

An app repository subclasses ``BaseRepository``, sets ``model``, and adds intent-
revealing query methods (``get_active_by_branch`` …) that bake in the right
``select_related``/``prefetch_related`` so the service layer never writes a raw
query or trips an N+1. Concrete repos are bound to their ``core.interfaces`` port
in ``core.bootstrap`` so services receive them by injection.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import models
from django.db.models import QuerySet

from core.interfaces import IBaseRepository


class BaseRepository[T: models.Model](IBaseRepository[T]):
    model: ClassVar[type[models.Model]]

    def get_queryset(self) -> QuerySet[T]:
        return self.model._default_manager.all()  # type: ignore[return-value]

    def get_by_id(self, pk: Any) -> T | None:
        return self.get_queryset().filter(pk=pk).first()

    def filter_by(self, **kwargs: Any) -> QuerySet[T]:
        return self.get_queryset().filter(**kwargs)

    def first(self, **kwargs: Any) -> T | None:
        qs = self.get_queryset()
        return (qs.filter(**kwargs) if kwargs else qs).first()

    def all(self) -> QuerySet[T]:
        return self.get_queryset()

    def create(self, **kwargs: Any) -> T:
        return self.get_queryset().create(**kwargs)

    def bulk_create(self, objects: list[T]) -> list[T]:
        return self.model._default_manager.bulk_create(objects)  # type: ignore[return-value]

    def update(self, instance: T, **kwargs: Any) -> T:
        for field, value in kwargs.items():
            setattr(instance, field, value)
        # Persist only the touched fields (cheaper write, no lost-update of
        # others), but include ``auto_now`` fields. Django only runs a field's
        # ``pre_save`` when it appears in update_fields; omitting updated_at made
        # every layered PATCH look stale to caches, sync clients, and operators.
        update_fields = list(kwargs)
        if update_fields:
            update_fields.extend(
                field.name
                for field in instance._meta.concrete_fields
                if getattr(field, "auto_now", False) and field.name not in update_fields
            )
            instance.save(update_fields=update_fields)
        else:
            instance.save()
        return instance

    def delete(self, instance: T) -> None:
        instance.delete()

    def exists(self, **kwargs: Any) -> bool:
        return self.get_queryset().filter(**kwargs).exists()

    def count(self, **kwargs: Any) -> int:
        qs = self.get_queryset()
        return (qs.filter(**kwargs) if kwargs else qs).count()

    def get_or_create(self, defaults: dict[str, Any] | None = None, **kwargs: Any) -> tuple[T, bool]:
        return self.get_queryset().get_or_create(defaults=defaults, **kwargs)
