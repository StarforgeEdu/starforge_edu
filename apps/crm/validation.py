from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.crm.dto import CRMScope, LeadOwnerDTO
from core.exceptions import ValidationException
from core.http import int_field, reject_unknown_fields, trimmed_str_field
from core.listing import validate_pagination_filters
from core.permissions import get_user_roles
from core.role_principals import STAFF_PRINCIPAL_KINDS
from core.scoping import permission_membership_scopes


def validate_query(request: HttpRequest, *, allowed: set[str], paginated: bool = False) -> None:
    unknown = sorted(set(request.GET) - allowed)
    if unknown:
        raise ValidationException(
            "Unknown query parameter.",
            fields={field: ["Unknown query parameter."] for field in unknown},
        )
    duplicates = sorted(name for name in request.GET if len(request.GET.getlist(name)) != 1)
    if duplicates:
        raise ValidationException(
            "Query parameter may be supplied only once.",
            fields={field: ["Supply this parameter once."] for field in duplicates},
        )
    if "search" in request.GET:
        term = request.GET["search"]
        if term != term.strip() or not 2 <= len(term) <= 200 or "\x00" in term:
            raise ValidationException(
                "Invalid search query.",
                fields={"search": ["Use 2 to 200 unpadded characters."]},
            )
    if paginated:
        validate_pagination_filters(request)


def crm_scope(request: HttpRequest, *, permission: str) -> CRMScope:
    if getattr(request.user, "is_superuser", False):
        return CRMScope(organization_wide=True)
    grants = permission_membership_scopes(
        roles=get_user_roles(request),
        permission=permission,
        account_kinds=STAFF_PRINCIPAL_KINDS,
    )
    return CRMScope(
        organization_wide=any(grant.is_organization_wide for grant in grants),
        branch_wide_ids=frozenset(
            grant.branch_id
            for grant in grants
            if not grant.is_organization_wide and grant.department_id is None
        ),
        department_scopes=frozenset(
            (grant.branch_id, grant.department_id)
            for grant in grants
            if not grant.is_organization_wide and grant.department_id is not None
        ),
    )


def owner_field(data: dict[str, Any], name: str = "owner", *, required: bool = False) -> LeadOwnerDTO | None:
    if name not in data:
        if required:
            raise ValidationException("Owner is required.", fields={name: ["This field is required."]})
        return None
    value = data[name]
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationException("Owner must be an object.", fields={name: ["Must be an object or null."]})
    reject_unknown_fields(value, allowed={"kind", "id"})
    kind = trimmed_str_field(value, "kind", required=True, max_length=16)
    principal_id = int_field(value, "id", required=True, min_value=1)
    if kind not in STAFF_PRINCIPAL_KINDS or principal_id is None:
        raise ValidationException(
            "Invalid owner.", fields={name: ["Choose a staff or teacher role account."]}
        )
    return LeadOwnerDTO(principal_kind=kind, principal_id=principal_id)


def iso_datetime_field(
    data: dict[str, Any],
    name: str,
    *,
    required: bool = False,
    past_limit_days: int | None = None,
    future_limit_days: int | None = None,
    future_limit_seconds: int | None = None,
) -> datetime | None:
    if name not in data or data[name] is None:
        if required:
            raise ValidationException(f"{name} is required.", fields={name: ["This field is required."]})
        return None
    raw = data[name]
    value = parse_datetime(raw) if isinstance(raw, str) else None
    if value is None or not timezone.is_aware(value):
        raise ValidationException(
            f"Invalid {name}.", fields={name: ["Use an ISO-8601 timestamp with an offset."]}
        )
    now = timezone.now()
    if past_limit_days is not None and value < now - timedelta(days=past_limit_days):
        raise ValidationException(
            f"Invalid {name}.", fields={name: [f"Must be within the last {past_limit_days} days."]}
        )
    future_limit = (
        timedelta(seconds=future_limit_seconds)
        if future_limit_seconds is not None
        else (timedelta(days=future_limit_days) if future_limit_days is not None else None)
    )
    if future_limit is not None and value > now + future_limit:
        description = (
            f"within {future_limit_seconds} seconds of the current time"
            if future_limit_seconds is not None
            else f"within the next {future_limit_days} days"
        )
        raise ValidationException(f"Invalid {name}.", fields={name: [f"Must be {description}."]})
    return value


def iso_date_field(data: dict[str, Any], name: str) -> date | None:
    if name not in data or data[name] is None:
        return None
    raw = data[name]
    if not isinstance(raw, str):
        raise ValidationException(f"Invalid {name}.", fields={name: ["Use YYYY-MM-DD."]})
    try:
        if len(raw) != 10:
            raise ValueError
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationException(f"Invalid {name}.", fields={name: ["Use YYYY-MM-DD."]}) from None


def idempotency_key(request: HttpRequest) -> str:
    value = request.headers.get("Idempotency-Key")
    if value is None:
        raise ValidationException(
            "Idempotency-Key is required.",
            code="invalid_idempotency_key",
            fields={"Idempotency-Key": ["This header is required."]},
        )
    return value
