"""Plain dict presenters for the platform control-center API (off DRF).

Replace the DRF ModelSerializers with explicit ``*_to_dict`` functions. These
run on the PUBLIC schema (platform staff view); ``success()`` renders the dicts
via ``DjangoJSONEncoder`` (datetimes -> ISO strings).
"""

from __future__ import annotations

from typing import Any

from .models import Center, Domain, DomainClaim


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def domain_to_dict(domain: Domain | DomainClaim) -> dict[str, Any]:
    is_claim = isinstance(domain, DomainClaim)
    data = {
        # Claims use UUIDs so their verify route cannot collide with the integer
        # ID accepted by the existing set-primary route.
        "id": str(domain.id) if is_claim else domain.id,
        "domain": domain.domain,
        "is_primary": domain.is_primary,
        "is_verified": not is_claim or domain.is_verified,
        "verified_at": _iso(domain.verified_at) if is_claim else None,
        "pending_primary": domain.pending_primary if is_claim else False,
        "record_type": "claim" if is_claim else "domain",
    }
    if is_claim and not domain.is_verified:
        data["verification"] = {
            "type": "TXT",
            "name": f"_starforge-verification.{domain.domain}",
            "value": f"starforge-domain-verification={domain.verification_token}",
        }
    return data


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
        "domains": [
            *[domain_to_dict(d) for d in center.domains.all()],
            *[
                domain_to_dict(claim)
                for claim in center.domain_claims.all()
                if claim.domain_record_id is None
            ],
        ],
    }
