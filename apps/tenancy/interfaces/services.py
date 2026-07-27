"""Service port for the tenancy (platform control-center) app."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.tenancy.models import Center, Domain


class ICenterService(ABC):
    # --- reads ---
    @abstractmethod
    def query(self) -> QuerySet[Center]:
        """Base Center queryset for the list view (filter/search/order applied by the view)."""

    @abstractmethod
    def get(self, pk: int) -> Center | None: ...

    @abstractmethod
    def usage(self, *, center: Center, days: int) -> dict[str, Any]: ...

    @abstractmethod
    def list_domains(self, *, center: Center) -> list[Domain]: ...

    @abstractmethod
    def resolve(self, *, slug: str) -> dict[str, Any]: ...

    # --- writes (delegate to the preserved domain functions) ---
    @abstractmethod
    def provision(self, *, data: dict[str, Any], actor: Any) -> Center: ...

    @abstractmethod
    def update_contact(self, *, center: Center, changes: dict[str, Any]) -> Center: ...

    @abstractmethod
    def suspend(self, *, center: Center, actor: Any, reason: str) -> Center: ...

    @abstractmethod
    def activate(self, *, center: Center, actor: Any) -> Center: ...

    @abstractmethod
    def extend_trial(self, *, center: Center, days: int, actor: Any) -> Center: ...

    @abstractmethod
    def add_domain(self, *, center: Center, domain_name: str, is_primary: bool) -> Domain: ...

    @abstractmethod
    def set_primary_domain(self, *, center: Center, domain_id: int) -> Domain: ...

    @abstractmethod
    def impersonate(self, *, center: Center, user_id: int, impersonator: Any) -> dict[str, Any]: ...
