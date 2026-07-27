"""Campaign endpoints (F10-1/2) — plain Django views over the layered architecture.

Three resources under /api/v1/campaigns/, all gated at the campaign:* codes:
- Campaigns (BRANCH-scoped): build against a student segment + send once (send is the
  campaign:send code); reception sees only its own branch, the director the whole centre.
- The do-not-contact list + message templates are UNSCOPED centre-wide tables.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.campaigns.dto.campaign_dto import CreateCampaignDTO, CreateTemplateDTO
from apps.campaigns.interfaces.services import (
    ICampaignService,
    IDoNotContactService,
    ITemplateService,
)
from apps.campaigns.presenters import (
    campaign_to_dict,
    do_not_contact_to_dict,
    recipient_to_dict,
    template_to_dict,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import Role, get_role_memberships, get_user_roles
from core.responses import created, error, no_content, paginated, success

_RESOURCE = "campaign"


def _campaign_service() -> ICampaignService:
    return container.resolve(ICampaignService)  # type: ignore[type-abstract]


def _dnc_service() -> IDoNotContactService:
    return container.resolve(IDoNotContactService)  # type: ignore[type-abstract]


def _template_service() -> ITemplateService:
    return container.resolve(ITemplateService)  # type: ignore[type-abstract]


def _scope(request: HttpRequest) -> tuple[bool, set[int]]:
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    is_unscoped = getattr(req.user, "is_superuser", False) or Role.DIRECTOR in roles
    branch_ids = {m.branch_id for m in get_role_memberships(req) if m.branch_id}
    return is_unscoped, branch_ids


def _get_campaign(request: HttpRequest, pk: int):
    is_unscoped, branch_ids = _scope(request)
    campaign = _campaign_service().get_visible(is_unscoped=is_unscoped, branch_ids=branch_ids, pk=pk)
    if campaign is None:
        raise NotFoundException(code="not_found")  # out-of-branch campaign -> 404, no leak
    return campaign


# --- campaigns -------------------------------------------------------------
@csrf_exempt
@require_auth
def campaigns_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        is_unscoped, branch_ids = _scope(request)
        qs = apply_filters(
            request,
            _campaign_service().scoped_list(is_unscoped=is_unscoped, branch_ids=branch_ids),
            filter_fields=("status", "branch"),
            ordering_fields=("created_at",),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([campaign_to_dict(c) for c in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create_campaign(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def campaign_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    return success(campaign_to_dict(_get_campaign(request, pk)))


@csrf_exempt
@require_auth
def campaign_send_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:send")
    campaign = _get_campaign(request, pk)
    return success(campaign_to_dict(_campaign_service().send(campaign_id=campaign.pk, actor=request.user)))


@csrf_exempt
@require_auth
def campaign_recipients_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    campaign = _get_campaign(request, pk)
    # Paginate: a center-wide campaign freezes one recipient row per targeted student
    # (thousands), so returning the whole set uncapped is unbounded. Mirrors the sibling
    # list endpoints; `data` stays the page's item list.
    qs = apply_filters(
        request,
        _campaign_service().recipients_of(campaign),
        filter_fields=("status",),
        ordering_fields=("id",),
        default_ordering="id",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([recipient_to_dict(r) for r in items], total=total, page=page, page_size=size)


def _create_campaign(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    name = str_field(body, "name", max_length=200).strip()
    if not name:
        raise ValidationException(
            "Name is required.", code="validation_error", fields={"name": ["This field is required."]}
        )
    segment = body.get("segment", {})
    if not isinstance(segment, dict):
        raise ValidationException(
            "segment must be an object.",
            code="validation_error",
            fields={"segment": ["Must be a {status?, cohort?} object."]},
        )
    dto = CreateCampaignDTO(
        name=name,
        message=str_field(body, "message"),
        template_id=int_field(body, "template"),
        branch_id=int_field(body, "branch"),
        segment=segment,
        scheduled_at=_datetime(body, "scheduled_at"),
    )
    is_unscoped, branch_ids = _scope(request)
    campaign = _campaign_service().create(
        dto, creator=request.user, is_unscoped=is_unscoped, branch_ids=branch_ids
    )
    return created(campaign_to_dict(campaign))


# --- do-not-contact --------------------------------------------------------
@csrf_exempt
@require_auth
def dnc_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = apply_filters(
            request,
            _dnc_service().list(),
            filter_fields=("phone",),
            search_fields=("phone",),
            ordering_fields=("created_at",),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([do_not_contact_to_dict(d) for d in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        entry = _dnc_service().create(
            phone=str_field(body, "phone", max_length=32),
            reason=str_field(body, "reason", max_length=255),
            actor=request.user,
        )
        return created(do_not_contact_to_dict(entry))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def dnc_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    entry = _dnc_service().get(pk)
    if entry is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(do_not_contact_to_dict(entry))
    if request.method == "DELETE":
        _dnc_service().delete(entry)  # opt back in
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


# --- message templates -----------------------------------------------------
@csrf_exempt
@require_auth
def templates_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        qs = apply_filters(
            request,
            _template_service().list(),
            filter_fields=("category", "is_active"),
            search_fields=("name", "category"),
            ordering_fields=("created_at", "name"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([template_to_dict(t) for t in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        body = read_json(request)
        name = str_field(body, "name", max_length=120).strip()
        if not name:
            raise ValidationException(
                "Name is required.", code="validation_error", fields={"name": ["This field is required."]}
            )
        dto = CreateTemplateDTO(
            name=name,
            category=str_field(body, "category", max_length=40),
            purpose=str_field(body, "purpose", max_length=500),
        )
        return created(template_to_dict(_template_service().create(dto, creator=request.user)))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def template_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    template = _template_service().get(pk)
    if template is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(template_to_dict(template))
    if request.method == "PATCH":
        return success(
            template_to_dict(_template_service().update(template, _template_changes(read_json(request))))
        )
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def template_generate_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    template = _template_service().get(pk)
    if template is None:
        raise NotFoundException(code="not_found")
    ai_request = _template_service().generate(template, requested_by=request.user)
    # 202 Accepted — the body is drafted async; poll /ai/requests/{id}/.
    return success({"request_id": ai_request.pk, "status": ai_request.status}, status=202)


def _datetime(body: dict[str, Any], name: str):
    """Parse an optional ISO-8601 datetime field; a bad value is a 400, never a 500.

    A naive value (no offset) is interpreted in the server timezone so the beat
    dispatcher's ``scheduled_at <= now`` comparison is always tz-aware."""
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    raw = body.get(name)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ValidationException(
            "Invalid datetime.", code="validation_error", fields={name: ["Must be an ISO datetime string."]}
        )
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValidationException(
            "Invalid datetime.",
            code="validation_error",
            fields={name: ["Must be an ISO-8601 datetime."]},
        )
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _template_changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "name" in body:
        name = str_field(body, "name", max_length=120)
        if not name.strip():
            raise ValidationException(
                "Name may not be blank.", code="validation_error", fields={"name": ["May not be blank."]}
            )
        changes["name"] = name
    if "category" in body:
        changes["category"] = str_field(body, "category", max_length=40)
    if "purpose" in body:
        changes["purpose"] = str_field(body, "purpose", max_length=500)
    if "body" in body:
        changes["body"] = str_field(body, "body")
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active")
    return changes
