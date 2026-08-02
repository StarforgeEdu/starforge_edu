from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_slug
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.crm.dto import (
    AttributionCreateDTO,
    CampaignCreateDTO,
    CRMScope,
    DuplicateReviewDTO,
    FollowUpCreateDTO,
    LeadCreateDTO,
    LeadFilterDTO,
    PipelineStageDTO,
    StageTransitionDTO,
    TouchCreateDTO,
)
from apps.crm.interfaces.services import ICRMService
from apps.crm.models import CRMLead, LeadFollowUp, LeadTouch, PipelineStage
from apps.crm.openapi_contracts import (
    ATTRIBUTION_CONTRACTS,
    CAMPAIGNS_COLLECTION_CONTRACTS,
    DETECT_DUPLICATES_CONTRACT,
    DUPLICATE_DISMISS_CONTRACT,
    DUPLICATE_MERGE_CONTRACT,
    DUPLICATES_COLLECTION_CONTRACTS,
    FOLLOW_UP_CONTRACTS,
    FOLLOW_UP_REGISTER_CONTRACTS,
    FUNNEL_CONTRACTS,
    LEAD_DETAIL_CONTRACTS,
    LEADS_COLLECTION_CONTRACTS,
    OWNER_CONTRACT,
    SOURCES_COLLECTION_CONTRACTS,
    STAGE_DETAIL_CONTRACTS,
    STAGE_HISTORY_CONTRACTS,
    STAGES_COLLECTION_CONTRACTS,
    TOUCH_CONTRACTS,
    TRANSITION_CONTRACT,
    follow_up_action_contract,
)
from apps.crm.presenters import (
    attribution_to_dict,
    campaign_to_dict,
    duplicate_to_dict,
    follow_up_to_dict,
    lead_to_dict,
    merge_to_dict,
    source_to_dict,
    stage_history_to_dict,
    stage_to_dict,
    touch_to_dict,
)
from apps.crm.validation import (
    crm_scope,
    idempotency_key,
    iso_date_field,
    iso_datetime_field,
    owner_field,
    validate_query,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, reject_unknown_fields, trimmed_str_field
from core.listing import apply_filters, paginate, positive_int_filter
from core.openapi_contracts import openapi_contract
from core.responses import error, paginated, success
from core.role_principals import STAFF_PRINCIPAL_KINDS, request_role_principal
from core.scoping import assert_permission_organization_scope

_READ = "crm:read"
_WRITE = "crm:write"
_MANAGE = "crm:manage"


def _service() -> ICRMService:
    return container.resolve(ICRMService)  # type: ignore[type-abstract]


def _actor(request: HttpRequest):
    return request_role_principal(request, allowed_kinds=STAFF_PRINCIPAL_KINDS)


def _mutation_response(data: Any, *, replayed: bool, created: bool = False) -> HttpResponse:
    response = success(data, status=200 if replayed or not created else 201)
    response["Idempotency-Replayed"] = "true" if replayed else "false"
    return response


def _required_positive(body: dict[str, Any], name: str) -> int:
    value = int_field(body, name, required=True, min_value=1)
    assert value is not None
    return value


def _choice(body: dict[str, Any], name: str, choices: list[str], *, required: bool = True) -> str:
    value = trimmed_str_field(body, name, required=required, max_length=64)
    if value not in choices:
        raise ValidationException(
            f"Invalid {name}.", fields={name: [f"Must be one of: {', '.join(choices)}."]}
        )
    return value


def _slug(body: dict[str, Any], name: str, *, max_length: int = 64) -> str:
    value = trimmed_str_field(body, name, required=True, max_length=max_length)
    if value != value.lower():
        raise ValidationException(
            f"Invalid {name}.",
            fields={name: ["Use lowercase letters, numbers, hyphens, and underscores."]},
        )
    try:
        validate_slug(value)
    except DjangoValidationError:
        raise ValidationException(
            f"Invalid {name}.", fields={name: ["Use letters, numbers, hyphens, and underscores."]}
        ) from None
    return value


def _query_date(request: HttpRequest, name: str) -> date | None:
    if name not in request.GET or request.GET.get(name) == "":
        return None
    raw = request.GET[name]
    try:
        if len(raw) != 10:
            raise ValueError
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationException(f"Invalid {name}.", fields={name: ["Use YYYY-MM-DD."]}) from None


def _date_bounds(lower: date | None, upper: date | None) -> tuple[datetime | None, datetime | None]:
    tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(lower, time.min), tz) if lower else None,
        timezone.make_aware(datetime.combine(upper, time.max), tz) if upper else None,
    )


def _require_boundary(scope: CRMScope, *, branch: int | None, department: int | None) -> None:
    if department is not None and branch is None:
        raise ValidationException(
            "Department requires branch.", fields={"department": ["Select branch as well."]}
        )
    if branch is not None and not scope.allows(branch_id=branch, department_id=department):
        raise ValidationException(
            "Choose a scope you can access.",
            fields={"branch": ["Choose a branch and department in your CRM scope."]},
        )


@openapi_contract(
    path="/api/v1/crm/stages/",
    operations=STAGES_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def stages_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, _READ)
        validate_query(
            request,
            allowed={"active", "search", "ordering", "page", "page_size"},
            paginated=True,
        )
        active_only = False
        if "active" in request.GET:
            active_only = bool_field({"active": request.GET["active"]}, "active")
        qs = apply_filters(
            request,
            _service().stages(active_only=active_only),
            search_fields=("slug", "name"),
            ordering_fields=("position", "name", "created_at"),
            default_ordering="position",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([stage_to_dict(row) for row in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, _MANAGE)
        assert_permission_organization_scope(request, permission=_MANAGE, account_kinds=STAFF_PRINCIPAL_KINDS)
        body = read_json(request)
        reject_unknown_fields(body, allowed={"slug", "name", "category", "position"})
        dto = PipelineStageDTO(
            slug=_slug(body, "slug"),
            name=trimmed_str_field(body, "name", required=True, max_length=120),
            category=_choice(body, "category", list(PipelineStage.Category.values)),
            position=_required_positive(body, "position"),
        )
        stage, replayed = _service().create_stage(
            dto,
            actor=request.user,
            actor_principal=_actor(request),
            idempotency_key=idempotency_key(request),
        )
        return _mutation_response(stage_to_dict(stage), replayed=replayed, created=True)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/crm/stages/{pk}/",
    operations=STAGE_DETAIL_CONTRACTS,
)
@csrf_exempt
@require_auth
def stage_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, _READ)
    elif request.method == "PATCH":
        check_perm(request, _MANAGE)
        assert_permission_organization_scope(
            request,
            permission=_MANAGE,
            account_kinds=STAFF_PRINCIPAL_KINDS,
        )
    else:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    stage = _service().stages().filter(pk=pk).first()
    if stage is None:
        raise NotFoundException(code="not_found")
    if request.method in ("GET", "HEAD"):
        validate_query(request, allowed=set())
        return success(stage_to_dict(stage))
    body = read_json(request)
    reject_unknown_fields(body, allowed={"slug", "name", "category", "position", "is_active"})
    if not body:
        raise ValidationException("At least one change is required.", fields={"body": ["Cannot be empty."]})
    changes: dict[str, Any] = {}
    if "slug" in body:
        changes["slug"] = _slug(body, "slug")
    if "name" in body:
        changes["name"] = trimmed_str_field(body, "name", required=True, max_length=120)
    if "category" in body:
        changes["category"] = _choice(body, "category", list(PipelineStage.Category.values))
    if "position" in body:
        changes["position"] = _required_positive(body, "position")
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active")
    return success(
        stage_to_dict(
            _service().update_stage(
                pk,
                changes,
                actor=request.user,
                actor_principal=_actor(request),
            )
        )
    )


@openapi_contract(
    path="/api/v1/crm/sources/",
    operations=SOURCES_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def sources_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, _READ)
        validate_query(
            request,
            allowed={"active", "search", "ordering", "page", "page_size"},
            paginated=True,
        )
        active_only = False
        if "active" in request.GET:
            active_only = bool_field({"active": request.GET["active"]}, "active")
        qs = apply_filters(
            request,
            _service().sources(active_only=active_only),
            search_fields=("slug", "name"),
            ordering_fields=("name", "created_at"),
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([source_to_dict(row) for row in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, _MANAGE)
        assert_permission_organization_scope(request, permission=_MANAGE, account_kinds=STAFF_PRINCIPAL_KINDS)
        body = read_json(request)
        reject_unknown_fields(body, allowed={"slug", "name"})
        source, replayed = _service().create_source(
            slug=_slug(body, "slug"),
            name=trimmed_str_field(body, "name", required=True, max_length=120),
            actor=request.user,
            actor_principal=_actor(request),
            idempotency_key=idempotency_key(request),
        )
        return _mutation_response(source_to_dict(source), replayed=replayed, created=True)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/crm/campaigns/",
    operations=CAMPAIGNS_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def campaigns_collection_view(request: HttpRequest) -> HttpResponse:
    permission = _READ if request.method in ("GET", "HEAD") else _WRITE
    check_perm(request, permission)
    scope = crm_scope(request, permission=permission)
    if request.method in ("GET", "HEAD"):
        validate_query(
            request,
            allowed={"active", "branch", "department", "source", "search", "ordering", "page", "page_size"},
            paginated=True,
        )
        active_only = False
        if "active" in request.GET:
            active_only = bool_field({"active": request.GET["active"]}, "active")
        branch = positive_int_filter(request, "branch")
        department = positive_int_filter(request, "department")
        _require_boundary(scope, branch=branch, department=department)
        qs = _service().campaigns(scope=scope, active_only=active_only)
        if branch is not None:
            qs = qs.filter(branch_id=branch)
        if department is not None:
            qs = qs.filter(department_id=department)
        source = positive_int_filter(request, "source")
        if source is not None:
            qs = qs.filter(source_id=source)
        qs = apply_filters(
            request,
            qs,
            search_fields=("code", "name"),
            ordering_fields=("created_at", "name", "starts_on"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([campaign_to_dict(item) for item in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        body = read_json(request)
        reject_unknown_fields(
            body,
            allowed={"code", "name", "source", "branch", "department", "starts_on", "ends_on"},
        )
        starts_on = iso_date_field(body, "starts_on")
        ends_on = iso_date_field(body, "ends_on")
        if starts_on and ends_on and starts_on > ends_on:
            raise ValidationException(
                "Campaign dates are reversed.", fields={"ends_on": ["Must be on or after starts_on."]}
            )
        campaign, replayed = _service().create_campaign(
            CampaignCreateDTO(
                code=_slug(body, "code"),
                name=trimmed_str_field(body, "name", required=True, max_length=160),
                source_id=_required_positive(body, "source"),
                branch_id=int_field(body, "branch", min_value=1),
                department_id=int_field(body, "department", min_value=1),
                starts_on=starts_on,
                ends_on=ends_on,
            ),
            scope=scope,
            actor=request.user,
            actor_principal=_actor(request),
            idempotency_key=idempotency_key(request),
        )
        return _mutation_response(campaign_to_dict(campaign), replayed=replayed, created=True)
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _lead_filters(request: HttpRequest, scope) -> LeadFilterDTO:
    validate_query(
        request,
        allowed={
            "branch",
            "department",
            "stage",
            "state",
            "owner_kind",
            "owner_id",
            "source",
            "campaign",
            "follow_up_from",
            "follow_up_to",
            "date_from",
            "date_to",
            "search",
            "ordering",
            "page",
            "page_size",
        },
        paginated=True,
    )
    branch = positive_int_filter(request, "branch")
    department = positive_int_filter(request, "department")
    _require_boundary(scope, branch=branch, department=department)
    stage = positive_int_filter(request, "stage")
    source = positive_int_filter(request, "source")
    campaign = positive_int_filter(request, "campaign")
    owner_kind = request.GET.get("owner_kind")
    owner_id = positive_int_filter(request, "owner_id")
    if (owner_kind is None) != (owner_id is None):
        raise ValidationException(
            "Owner filters must be paired.",
            fields={
                "owner_kind": ["Provide owner_kind and owner_id together."],
                "owner_id": ["Provide owner_kind and owner_id together."],
            },
        )
    if owner_kind is not None and owner_kind not in STAFF_PRINCIPAL_KINDS:
        raise ValidationException("Invalid owner kind.", fields={"owner_kind": ["Must be staff or teacher."]})
    state = request.GET.get("state")
    if state is not None and state not in CRMLead.State.values:
        raise ValidationException(
            "Invalid state.", fields={"state": [f"Must be one of: {', '.join(CRMLead.State.values)}."]}
        )
    follow_from, follow_to = _query_date(request, "follow_up_from"), _query_date(request, "follow_up_to")
    created_from, created_to = _query_date(request, "date_from"), _query_date(request, "date_to")
    if follow_from and follow_to and follow_from > follow_to:
        raise ValidationException(
            "Follow-up dates are reversed.", fields={"follow_up_to": ["Must be on or after follow_up_from."]}
        )
    if created_from and created_to and created_from > created_to:
        raise ValidationException(
            "Created dates are reversed.", fields={"date_to": ["Must be on or after date_from."]}
        )
    follow_lower, follow_upper = _date_bounds(follow_from, follow_to)
    created_lower, created_upper = _date_bounds(created_from, created_to)
    return LeadFilterDTO(
        branch_id=branch,
        department_id=department,
        stage_id=stage,
        state=state,
        owner_kind=owner_kind,
        owner_id=owner_id,
        source_id=source,
        campaign_id=campaign,
        follow_up_from=follow_lower,
        follow_up_to=follow_upper,
        created_from=created_lower,
        created_to=created_upper,
    )


@openapi_contract(
    path="/api/v1/crm/leads/",
    operations=LEADS_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def leads_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, _READ)
        scope = crm_scope(request, permission=_READ)
        filters = _lead_filters(request, scope)
        qs = apply_filters(
            request,
            _service().leads(scope=scope, filters=filters),
            search_fields=(
                "student__student_id",
                "student__first_name",
                "student__last_name",
                "student__phone",
                "student__email",
            ),
            ordering_fields=("created_at", "updated_at", "next_follow_up_at"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([lead_to_dict(item) for item in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, _WRITE)
        scope = crm_scope(request, permission=_WRITE)
        body = read_json(request)
        reject_unknown_fields(
            body,
            allowed={
                "student",
                "stage",
                "department",
                "owner",
                "source",
                "campaign",
                "medium",
                "content",
                "attribution_occurred_at",
            },
        )
        dto = LeadCreateDTO(
            student_id=_required_positive(body, "student"),
            stage_id=_required_positive(body, "stage"),
            department_id=int_field(body, "department", min_value=1),
            owner=owner_field(body),
            source_id=_required_positive(body, "source"),
            campaign_id=int_field(body, "campaign", min_value=1),
            medium=trimmed_str_field(body, "medium", max_length=64),
            content=trimmed_str_field(body, "content", max_length=160),
            attribution_occurred_at=iso_datetime_field(
                body,
                "attribution_occurred_at",
                past_limit_days=3650,
                future_limit_seconds=300,
            ),
        )
        lead, replayed = _service().create_lead(
            dto,
            scope=scope,
            actor=request.user,
            actor_principal=_actor(request),
            idempotency_key=idempotency_key(request),
        )
        return _mutation_response(lead_to_dict(lead), replayed=replayed, created=True)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/crm/leads/{pk}/",
    operations=LEAD_DETAIL_CONTRACTS,
)
@csrf_exempt
@require_auth
def lead_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _READ)
    validate_query(request, allowed=set())
    lead = _service().get_lead(scope=crm_scope(request, permission=_READ), pk=pk)
    if lead is None:
        raise NotFoundException(code="not_found")
    return success(lead_to_dict(lead))


@openapi_contract(
    path="/api/v1/crm/leads/{pk}/owner/",
    operations=(OWNER_CONTRACT,),
)
@csrf_exempt
@require_auth
def lead_owner_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _WRITE)
    body = read_json(request)
    reject_unknown_fields(body, allowed={"owner"})
    owner = owner_field(body, required=True)
    lead, replayed = _service().assign_owner(
        pk,
        owner,
        scope=crm_scope(request, permission=_WRITE),
        actor=request.user,
        actor_principal=_actor(request),
        idempotency_key=idempotency_key(request),
    )
    return _mutation_response(lead_to_dict(lead), replayed=replayed)


@openapi_contract(
    path="/api/v1/crm/leads/{pk}/transition/",
    operations=(TRANSITION_CONTRACT,),
)
@csrf_exempt
@require_auth
def lead_transition_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _WRITE)
    body = read_json(request)
    reject_unknown_fields(body, allowed={"stage", "expected_version", "loss_reason", "note"})
    dto = StageTransitionDTO(
        stage_id=_required_positive(body, "stage"),
        expected_version=_required_positive(body, "expected_version"),
        loss_reason=trimmed_str_field(body, "loss_reason", max_length=255),
        note=trimmed_str_field(body, "note", max_length=1000),
    )
    history, replayed = _service().transition(
        pk,
        dto,
        scope=crm_scope(request, permission=_WRITE),
        actor=request.user,
        actor_principal=_actor(request),
        idempotency_key=idempotency_key(request),
    )
    return _mutation_response(stage_history_to_dict(history), replayed=replayed)


def _lead_for_timeline(request: HttpRequest, pk: int, *, permission: str):
    lead = _service().get_lead(scope=crm_scope(request, permission=permission), pk=pk)
    if lead is None:
        raise NotFoundException(code="not_found")
    return lead


@openapi_contract(
    path="/api/v1/crm/leads/{pk}/stage-history/",
    operations=STAGE_HISTORY_CONTRACTS,
)
@csrf_exempt
@require_auth
def lead_stage_history_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _READ)
    validate_query(request, allowed={"page", "page_size"}, paginated=True)
    lead = _lead_for_timeline(request, pk, permission=_READ)
    items, total, page, size = paginate(request, _service().stage_history(lead))
    return paginated([stage_history_to_dict(item) for item in items], total=total, page=page, page_size=size)


@openapi_contract(
    path="/api/v1/crm/leads/{pk}/touches/",
    operations=TOUCH_CONTRACTS,
)
@csrf_exempt
@require_auth
def lead_touches_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, _READ)
        validate_query(
            request,
            allowed={"channel", "direction", "date_from", "date_to", "page", "page_size"},
            paginated=True,
        )
        lead = _lead_for_timeline(request, pk, permission=_READ)
        qs = _service().touches(lead)
        channel = request.GET.get("channel")
        direction = request.GET.get("direction")
        if channel is not None and channel not in LeadTouch.Channel.values:
            raise ValidationException("Invalid channel.", fields={"channel": ["Invalid value."]})
        if direction is not None and direction not in LeadTouch.Direction.values:
            raise ValidationException("Invalid direction.", fields={"direction": ["Invalid value."]})
        if channel:
            qs = qs.filter(channel=channel)
        if direction:
            qs = qs.filter(direction=direction)
        lower, upper = _query_date(request, "date_from"), _query_date(request, "date_to")
        if lower and upper and lower > upper:
            raise ValidationException("Dates are reversed.", fields={"date_to": ["Must follow date_from."]})
        lower_dt, upper_dt = _date_bounds(lower, upper)
        if lower_dt:
            qs = qs.filter(occurred_at__gte=lower_dt)
        if upper_dt:
            qs = qs.filter(occurred_at__lte=upper_dt)
        items, total, page, size = paginate(request, qs)
        return paginated([touch_to_dict(item) for item in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, _WRITE)
        body = read_json(request)
        reject_unknown_fields(body, allowed={"channel", "direction", "outcome", "summary", "occurred_at"})
        dto = TouchCreateDTO(
            channel=_choice(body, "channel", list(LeadTouch.Channel.values)),
            direction=_choice(body, "direction", list(LeadTouch.Direction.values)),
            outcome=trimmed_str_field(body, "outcome", max_length=64),
            summary=trimmed_str_field(body, "summary", required=True, max_length=2000),
            occurred_at=iso_datetime_field(
                body,
                "occurred_at",
                past_limit_days=3650,
                future_limit_seconds=300,
            ),
        )
        touch, replayed = _service().add_touch(
            pk,
            dto,
            scope=crm_scope(request, permission=_WRITE),
            actor=request.user,
            actor_principal=_actor(request),
            idempotency_key=idempotency_key(request),
        )
        return _mutation_response(touch_to_dict(touch), replayed=replayed, created=True)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/crm/leads/{pk}/follow-ups/",
    operations=FOLLOW_UP_CONTRACTS,
)
@csrf_exempt
@require_auth
def lead_follow_ups_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, _READ)
        validate_query(request, allowed={"status", "page", "page_size"}, paginated=True)
        lead = _lead_for_timeline(request, pk, permission=_READ)
        qs = _service().follow_ups(lead)
        status = request.GET.get("status")
        if status is not None and status not in LeadFollowUp.Status.values:
            raise ValidationException("Invalid status.", fields={"status": ["Invalid value."]})
        if status:
            qs = qs.filter(status=status)
        items, total, page, size = paginate(request, qs)
        return paginated([follow_up_to_dict(item) for item in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, _WRITE)
        body = read_json(request)
        reject_unknown_fields(body, allowed={"due_at", "purpose", "assignee"})
        due_at = iso_datetime_field(body, "due_at", required=True, future_limit_days=1825)
        assert due_at is not None
        follow_up, replayed = _service().add_follow_up(
            pk,
            FollowUpCreateDTO(
                due_at=due_at,
                purpose=trimmed_str_field(body, "purpose", required=True, max_length=500),
                assignee=owner_field(body, "assignee"),
            ),
            scope=crm_scope(request, permission=_WRITE),
            actor=request.user,
            actor_principal=_actor(request),
            idempotency_key=idempotency_key(request),
        )
        return _mutation_response(follow_up_to_dict(follow_up), replayed=replayed, created=True)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/crm/follow-ups/",
    operations=FOLLOW_UP_REGISTER_CONTRACTS,
)
@csrf_exempt
@require_auth
def follow_up_register_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _READ)
    validate_query(
        request,
        allowed={
            "branch",
            "department",
            "status",
            "assignee_kind",
            "assignee_id",
            "due_from",
            "due_to",
            "search",
            "ordering",
            "page",
            "page_size",
        },
        paginated=True,
    )
    scope = crm_scope(request, permission=_READ)
    branch = positive_int_filter(request, "branch")
    department = positive_int_filter(request, "department")
    _require_boundary(scope, branch=branch, department=department)
    qs = _service().follow_up_register(scope=scope)
    if branch is not None:
        qs = qs.filter(lead__branch_id=branch)
    if department is not None:
        qs = qs.filter(lead__department_id=department)
    status = request.GET.get("status")
    if status is not None and status not in LeadFollowUp.Status.values:
        raise ValidationException("Invalid status.", fields={"status": ["Invalid value."]})
    if status:
        qs = qs.filter(status=status)
    assignee_kind = request.GET.get("assignee_kind")
    assignee_id = positive_int_filter(request, "assignee_id")
    if (assignee_kind is None) != (assignee_id is None):
        raise ValidationException(
            "Assignee filters must be paired.",
            fields={
                "assignee_kind": ["Provide assignee_kind and assignee_id together."],
                "assignee_id": ["Provide assignee_kind and assignee_id together."],
            },
        )
    if assignee_kind is not None:
        if assignee_kind not in STAFF_PRINCIPAL_KINDS:
            raise ValidationException(
                "Invalid assignee kind.", fields={"assignee_kind": ["Must be staff or teacher."]}
            )
        qs = qs.filter(
            assignee_principal_kind=assignee_kind,
            assignee_principal_id=assignee_id,
        )
    due_from, due_to = _query_date(request, "due_from"), _query_date(request, "due_to")
    if due_from and due_to and due_from > due_to:
        raise ValidationException(
            "Follow-up dates are reversed.", fields={"due_to": ["Must be on or after due_from."]}
        )
    lower, upper = _date_bounds(due_from, due_to)
    if lower is not None:
        qs = qs.filter(due_at__gte=lower)
    if upper is not None:
        qs = qs.filter(due_at__lte=upper)
    qs = apply_filters(
        request,
        qs,
        search_fields=(
            "purpose",
            "lead__student__student_id",
            "lead__student__first_name",
            "lead__student__last_name",
        ),
        ordering_fields=("due_at", "created_at", "updated_at"),
        default_ordering="due_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated(
        [follow_up_to_dict(item) for item in items],
        total=total,
        page=page,
        page_size=size,
    )


def _follow_up_resolve(request: HttpRequest, pk: int, action: str, body: dict[str, Any]) -> HttpResponse:
    if request.method != "POST" or action not in {"complete", "cancel"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _WRITE)
    reject_unknown_fields(body, allowed={"note"})
    status = LeadFollowUp.Status.COMPLETED if action == "complete" else LeadFollowUp.Status.CANCELLED
    follow_up, replayed = _service().resolve_follow_up(
        pk,
        status=status,
        note=trimmed_str_field(body, "note", max_length=1000),
        scope=crm_scope(request, permission=_WRITE),
        actor=request.user,
        actor_principal=_actor(request),
        idempotency_key=idempotency_key(request),
    )
    return _mutation_response(follow_up_to_dict(follow_up), replayed=replayed)


@openapi_contract(
    path="/api/v1/crm/follow-ups/{pk}/complete/",
    operations=(follow_up_action_contract("complete"),),
)
@csrf_exempt
@require_auth
def follow_up_complete_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return _follow_up_resolve(request, pk, "complete", read_json(request))


@openapi_contract(
    path="/api/v1/crm/follow-ups/{pk}/cancel/",
    operations=(follow_up_action_contract("cancel"),),
)
@csrf_exempt
@require_auth
def follow_up_cancel_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return _follow_up_resolve(request, pk, "cancel", read_json(request))


@openapi_contract(
    path="/api/v1/crm/leads/{pk}/attributions/",
    operations=ATTRIBUTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def lead_attributions_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, _READ)
        validate_query(request, allowed={"source", "campaign", "page", "page_size"}, paginated=True)
        lead = _lead_for_timeline(request, pk, permission=_READ)
        qs = _service().attributions(lead)
        source = positive_int_filter(request, "source")
        campaign = positive_int_filter(request, "campaign")
        if source is not None:
            qs = qs.filter(source_id=source)
        if campaign is not None:
            qs = qs.filter(campaign_id=campaign)
        items, total, page, size = paginate(request, qs)
        return paginated(
            [attribution_to_dict(item) for item in items], total=total, page=page, page_size=size
        )
    if request.method == "POST":
        check_perm(request, _WRITE)
        body = read_json(request)
        reject_unknown_fields(body, allowed={"source", "campaign", "medium", "content", "occurred_at"})
        attribution, replayed = _service().add_attribution(
            pk,
            AttributionCreateDTO(
                source_id=_required_positive(body, "source"),
                campaign_id=int_field(body, "campaign", min_value=1),
                medium=trimmed_str_field(body, "medium", max_length=64),
                content=trimmed_str_field(body, "content", max_length=160),
                occurred_at=iso_datetime_field(
                    body,
                    "occurred_at",
                    past_limit_days=3650,
                    future_limit_seconds=300,
                ),
            ),
            scope=crm_scope(request, permission=_WRITE),
            actor=request.user,
            actor_principal=_actor(request),
            idempotency_key=idempotency_key(request),
        )
        return _mutation_response(attribution_to_dict(attribution), replayed=replayed, created=True)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@openapi_contract(
    path="/api/v1/crm/leads/{pk}/detect-duplicates/",
    operations=(DETECT_DUPLICATES_CONTRACT,),
)
@csrf_exempt
@require_auth
def lead_duplicate_detect_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _WRITE)
    reject_unknown_fields(read_json(request), allowed=set())
    candidates, replayed = _service().detect_duplicates(
        pk,
        scope=crm_scope(request, permission=_WRITE),
        actor=request.user,
        actor_principal=_actor(request),
        idempotency_key=idempotency_key(request),
    )
    return _mutation_response([duplicate_to_dict(item) for item in candidates], replayed=replayed)


@openapi_contract(
    path="/api/v1/crm/duplicates/",
    operations=DUPLICATES_COLLECTION_CONTRACTS,
)
@csrf_exempt
@require_auth
def duplicates_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _READ)
    validate_query(request, allowed={"status", "ordering", "page", "page_size"}, paginated=True)
    qs = _service().duplicates(scope=crm_scope(request, permission=_READ))
    status = request.GET.get("status")
    if status is not None and status not in {"pending", "dismissed", "merged"}:
        raise ValidationException("Invalid status.", fields={"status": ["Invalid value."]})
    if status:
        qs = qs.filter(status=status)
    qs = apply_filters(
        request,
        qs,
        ordering_fields=("score", "detected_at", "reviewed_at"),
        default_ordering="-score",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([duplicate_to_dict(item) for item in items], total=total, page=page, page_size=size)


def _duplicate_review(request: HttpRequest, pk: int, action: str, body: dict[str, Any]) -> HttpResponse:
    if request.method != "POST" or action not in {"dismiss", "merge"}:
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _WRITE)
    allowed = {"rationale", "canonical_lead"} if action == "merge" else {"rationale"}
    reject_unknown_fields(body, allowed=allowed)
    dto = DuplicateReviewDTO(
        rationale=trimmed_str_field(body, "rationale", required=True, max_length=1000),
        canonical_lead_id=(_required_positive(body, "canonical_lead") if action == "merge" else None),
    )
    kwargs = {
        "scope": crm_scope(request, permission=_WRITE),
        "actor": request.user,
        "actor_principal": _actor(request),
        "idempotency_key": idempotency_key(request),
    }
    if action == "merge":
        result, replayed = _service().merge_duplicate(pk, dto, **kwargs)
        return _mutation_response(merge_to_dict(result), replayed=replayed)
    result, replayed = _service().dismiss_duplicate(pk, dto, **kwargs)
    return _mutation_response(duplicate_to_dict(result), replayed=replayed)


@openapi_contract(
    path="/api/v1/crm/duplicates/{pk}/dismiss/",
    operations=(DUPLICATE_DISMISS_CONTRACT,),
)
@csrf_exempt
@require_auth
def duplicate_dismiss_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return _duplicate_review(request, pk, "dismiss", read_json(request))


@openapi_contract(
    path="/api/v1/crm/duplicates/{pk}/merge/",
    operations=(DUPLICATE_MERGE_CONTRACT,),
)
@csrf_exempt
@require_auth
def duplicate_merge_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return _duplicate_review(request, pk, "merge", read_json(request))


@openapi_contract(
    path="/api/v1/crm/funnel/",
    operations=FUNNEL_CONTRACTS,
)
@csrf_exempt
@require_auth
def funnel_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, _READ)
    validate_query(
        request,
        allowed={"date_from", "date_to", "branch", "department", "source", "campaign"},
    )
    date_from = _query_date(request, "date_from")
    date_to = _query_date(request, "date_to")
    if date_from is None or date_to is None:
        raise ValidationException(
            "Funnel window is required.",
            fields={
                "date_from": ["Provide the inclusive start date."],
                "date_to": ["Provide the inclusive end date."],
            },
        )
    if date_from > date_to:
        raise ValidationException(
            "Funnel dates are reversed.", fields={"date_to": ["Must follow date_from."]}
        )
    if date_to - date_from > timedelta(days=365):
        raise ValidationException(
            "Funnel window is too large.", fields={"date_to": ["Window may not exceed 366 days."]}
        )
    return success(
        _service().funnel(
            scope=crm_scope(request, permission=_READ),
            date_from=date_from,
            date_to=date_to,
            branch_id=positive_int_filter(request, "branch"),
            department_id=positive_int_filter(request, "department"),
            source_id=positive_int_filter(request, "source"),
            campaign_id=positive_int_filter(request, "campaign"),
        )
    )
