"""Platform control-center service — thin orchestration over the preserved
tenancy domain functions (schema lifecycle) + the Center read repository.

The heavy lifting (schema creation, the ALTER SCHEMA archival, the row-locked
domain promotion, impersonation-token minting, the PlatformEvent audit trail)
stays VERBATIM in ``apps.tenancy.services`` (the package __init__), which is
imported by billing + the celery tenancy tasks. This class just adapts it to
the layered view/DI style and records the CENTER_CREATED event on provision.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.billing import selectors as billing_selectors
from apps.tenancy import services as domain
from apps.tenancy.interfaces.repositories import ICenterRepository
from apps.tenancy.interfaces.services import ICenterService
from apps.tenancy.models import Center, Domain


class CenterService(ICenterService):
    def __init__(self, center_repository: ICenterRepository) -> None:
        self._centers = center_repository

    # --- reads ---
    def query(self) -> QuerySet[Center]:
        return self._centers.query()

    def get(self, pk: int) -> Center | None:
        return self._centers.get(pk)

    def usage(self, *, center: Center, days: int) -> dict[str, Any]:
        return billing_selectors.usage_series(center=center, days=days)

    def list_domains(self, *, center: Center) -> list[Domain]:
        return list(center.domains.all())

    def resolve(self, *, slug: str) -> dict[str, Any]:
        return domain.resolve_tenant(slug=slug)

    # --- writes (delegate to the preserved domain functions) ---
    def provision(self, *, data: dict[str, Any], actor: Any) -> Center:
        center = domain.provision_center(**data)
        domain.record_platform_event(
            actor=actor,
            center=center,
            event=domain.PlatformEvent.Event.CENTER_CREATED,
            payload={"slug": center.slug},
        )
        # Re-fetch so the response presenter renders the prefetched domains.
        return self._centers.get(center.pk) or center

    def update_contact(self, *, center: Center, changes: dict[str, Any]) -> Center:
        for field, value in changes.items():
            setattr(center, field, value)
        if changes:
            center.save(update_fields=[*changes.keys(), "updated_at"])
        return self._centers.get(center.pk) or center

    def suspend(self, *, center: Center, actor: Any, reason: str) -> Center:
        return domain.suspend_center(center, actor=actor, reason=reason)

    def activate(self, *, center: Center, actor: Any) -> Center:
        return domain.activate_center(center, actor=actor)

    def extend_trial(self, *, center: Center, days: int, actor: Any) -> Center:
        return domain.extend_trial(center, days=days, actor=actor)

    def add_domain(self, *, center: Center, domain_name: str, is_primary: bool) -> Domain:
        return domain.add_domain(center, domain=domain_name, is_primary=is_primary)

    def set_primary_domain(self, *, center: Center, domain_id: int) -> Domain:
        return domain.set_primary_domain(center, domain_id)

    def impersonate(self, *, center: Center, user_id: int, impersonator: Any) -> dict[str, Any]:
        return domain.mint_impersonation_token(center=center, user_id=user_id, impersonator=impersonator)
