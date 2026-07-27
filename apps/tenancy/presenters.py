"""Plain dict presenters for the platform control-center API (off DRF).

Replace the DRF ModelSerializers with explicit ``*_to_dict`` functions. These
run on the PUBLIC schema (platform staff view); ``success()`` renders the dicts
via ``DjangoJSONEncoder`` (datetimes -> ISO strings).
"""

from __future__ import annotations

from typing import Any

from .models import Center, Domain


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def domain_to_dict(domain: Domain) -> dict[str, Any]:
    return {
        "id": domain.id,
        "domain": domain.domain,
        "is_primary": domain.is_primary,
    }


def center_to_dict(center: Center) -> dict[str, Any]:
    """Mirror the old ``CenterSerializer`` field-for-field (incl. nested domains)."""
    return {
        "id": center.id,
        "name": center.name,
        "slug": center.slug,
        "schema_name": center.schema_name,
        "contact_name": center.contact_name,
        "contact_phone": center.contact_phone,
        "contact_email": center.contact_email,
        "is_active": center.is_active,
        "on_trial": center.on_trial,
        "trial_ends_at": _iso(center.trial_ends_at),
        "archived_at": _iso(center.archived_at),
        "created_at": _iso(center.created_at),
        "domains": [domain_to_dict(d) for d in center.domains.all()],
    }
