"""Intelligence HTTP views (layered, off DRF).

Read-only A-3 facets (transparent rules, no black box): executive snapshot, dropout-risk list +
detail, branch ranking, family-health retention feed, a student's journey timeline,
the risk rules, and teacher engagement. All are GET. Every facet is scoped in the
view (which students/branches/teachers the caller may see) and rendered from the
preserved apps.intelligence.selectors read layer via IIntelligenceService.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.utils import timezone, translation
from django.utils.cache import patch_vary_headers
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.intelligence.cache import (
    CachePolicy,
    CacheResult,
    apply_cache_headers,
    get_or_compute,
    intelligence_cache_key,
)
from apps.intelligence.dto import ExecutiveSummaryContext
from apps.intelligence.executive import (
    executive_cache_key,
    included_executive_sections,
    parse_executive_query,
    resolve_executive_scope,
)
from apps.intelligence.interfaces.services import IIntelligenceService
from apps.intelligence.openapi_contracts import (
    EXECUTIVE_GET_CONTRACT,
    EXECUTIVE_HEAD_CONTRACT,
)
from apps.org.models import Branch
from apps.org.selectors import get_center_settings
from apps.students.models import StudentProfile
from apps.students.selectors import scoped_students
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.listing import (
    DEFAULT_PAGE_SIZE,
    paginate_sequence,
    positive_int_filter,
    validate_pagination_filters,
)
from core.openapi_contracts import openapi_contract
from core.permissions import (
    _request_overrides,
    get_user_roles,
    has_permission_code,
)
from core.responses import error, success
from core.role_principals import STAFF_PRINCIPAL_KINDS, request_role_principal
from core.scoping import (
    is_permission_unscoped,
    permission_membership_branch_wide_ids,
    permission_membership_scope_q,
    permission_membership_scopes,
    request_permission_membership_allows,
)
from core.utils import stable_hash

logger = logging.getLogger("starforge.intelligence")

# Only STAFF memberships grant a branch scope for the intelligence facets — a
# student/parent membership must never (e.g. via an A-2 grant of intelligence:read)
# resolve to a branch and open the branch-level feeds. This fails closed for them.


def _service() -> IIntelligenceService:
    return container.resolve(IIntelligenceService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _validate_query(request: HttpRequest, *, allowed: set[str]) -> None:
    """Reject ambiguous or misspelled decision-register selectors."""
    unknown = sorted(set(request.GET) - allowed)
    if unknown:
        raise ValidationException(
            "Unknown query parameter.",
            code="validation_error",
            fields={field: ["Unknown query parameter."] for field in unknown},
        )
    duplicates = sorted(name for name in request.GET if len(request.GET.getlist(name)) != 1)
    if duplicates:
        raise ValidationException(
            "Query parameter may be supplied only once.",
            code="validation_error",
            fields={field: ["Supply this parameter once."] for field in duplicates},
        )
    validate_pagination_filters(request)


def _page_results(request: HttpRequest, payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results", [])
    rows, total, page, page_size = paginate_sequence(request, results)
    return {
        **payload,
        "count": total,
        "results": rows,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


def _page_values(request: HttpRequest) -> tuple[int, int]:
    return (
        positive_int_filter(request, "page") or 1,
        positive_int_filter(request, "page_size") or DEFAULT_PAGE_SIZE,
    )


def _executive_response(
    request: HttpRequest,
    cached: CacheResult,
) -> HttpResponse:
    """Return a private conditional response for one already-scoped payload."""

    payload = cached.payload
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    etag = f'"{stable_hash(encoded)}"'
    if _if_none_match(request.headers.get("If-None-Match", ""), etag):
        response = HttpResponse(status=304)
    else:
        response = success(payload)
    response["ETag"] = etag
    # The server-side snapshot cache is short-lived, but a management browser
    # must revalidate on every navigation so revoking its bearer session cannot
    # leave a fresh protected response reusable from the browser cache.
    response["Cache-Control"] = "private, no-cache, max-age=0, must-revalidate"
    patch_vary_headers(response, ("Accept-Language", "Authorization"))
    apply_cache_headers(response, cached)
    return response


def _private_cached_response(cached: CacheResult) -> HttpResponse:
    """Render a protected cached payload without enabling browser reuse."""

    response = success(cached.payload)
    response["Cache-Control"] = "private, no-cache, max-age=0, must-revalidate"
    patch_vary_headers(response, ("Accept-Language", "Authorization"))
    apply_cache_headers(response, cached)
    return response


def _executive_cache_policy() -> CachePolicy:
    return CachePolicy(
        fresh_seconds=int(settings.EXECUTIVE_SUMMARY_CACHE_FRESH_SECONDS),
        stale_seconds=int(settings.EXECUTIVE_SUMMARY_CACHE_STALE_SECONDS),
        lock_seconds=int(settings.INTELLIGENCE_CACHE_LOCK_SECONDS),
    )


def _risk_cache_policy() -> CachePolicy:
    return CachePolicy(
        fresh_seconds=int(settings.INTELLIGENCE_RISK_CACHE_FRESH_SECONDS),
        stale_seconds=int(settings.INTELLIGENCE_RISK_CACHE_STALE_SECONDS),
        lock_seconds=int(settings.INTELLIGENCE_CACHE_LOCK_SECONDS),
    )


def _if_none_match(header: str, etag: str) -> bool:
    """Weak comparison is correct for GET/HEAD ``If-None-Match``."""

    if not header:
        return False
    expected = etag.removeprefix("W/")
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == expected:
            return True
    return False


def _can_see_finance(request: HttpRequest) -> bool:
    """Whether to include the overdue-payment flag — only callers who may see
    finance (finance:read / superuser) get the financial signal."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    if req.user.is_superuser:
        return True
    roles = get_user_roles(req)
    if is_permission_unscoped(request, permission="finance:read"):
        return True
    if not has_permission_code(roles, "finance:read", _request_overrides(req)):
        return False

    # The selector accepts one finance flag for the entire result set. Require
    # finance scope to cover every intelligence scope so a grant in Branch B
    # cannot expose overdue-payment flags for Branch A.
    intelligence_scopes = permission_membership_scopes(
        roles=roles,
        permission="intelligence:read",
        account_kinds={"staff", "teacher"},
    )
    finance_scopes = permission_membership_scopes(
        roles=roles,
        permission="finance:read",
        account_kinds={"staff", "teacher"},
    )
    return bool(intelligence_scopes) and all(
        any(
            finance.branch_id == intelligence.branch_id
            # Legacy risk/family selectors accept one finance flag but do not
            # carry immutable department boundaries into their invoice
            # subqueries. Only a branch-wide finance grant is therefore safe;
            # an exact department grant must not become a same-branch arrears
            # oracle. The executive summary has its own exact snapshot scope.
            and finance.department_id is None
            for finance in finance_scopes
        )
        for intelligence in intelligence_scopes
    )


def _branch_wide_permission_intersection(
    request: HttpRequest,
    *permissions: str,
) -> set[int] | None:
    """Exact branch intersection, or ``None`` for organization-wide access."""
    if request.user.is_superuser:
        return None
    roles = get_user_roles(request)
    allowed: set[int] | None = None
    for permission in permissions or ("intelligence:read",):
        if is_permission_unscoped(
            request,
            permission=permission,
            account_kinds={"staff", "teacher"},
        ):
            continue
        branch_ids = permission_membership_branch_wide_ids(
            roles=roles,
            permission=permission,
            account_kinds={"staff", "teacher"},
        )
        allowed = branch_ids if allowed is None else allowed & branch_ids
    return allowed


def _scoped_branches(request: HttpRequest, *permissions: str):
    """Branches covered branch-wide by every requested permission.

    Branch ranking and family-health selectors have no department dimension. A
    department-only membership therefore cannot be safely widened to its whole
    branch. Multiple permissions are intersected membership-by-membership so a
    parents grant in Branch B cannot be borrowed for intelligence in Branch A.
    """
    qs = Branch.objects.filter(archived_at__isnull=True)
    allowed = _branch_wide_permission_intersection(request, *permissions)
    return qs if allowed is None else qs.filter(pk__in=allowed)


def _scoped_teachers(request: HttpRequest):
    """Teachers whose engagement the caller may see: director/superuser → all; a
    manager (HOD) → their branch(es)' teachers; a teacher → only their own row;
    anyone else → none (fail closed, even with an A-2 intelligence:read grant)."""
    from apps.teachers.models import TeacherProfile
    from apps.teachers.selectors import teacher_profile_for

    base = TeacherProfile.objects.select_related("user")
    roles = get_user_roles(request)
    if is_permission_unscoped(request, permission="intelligence:read"):
        return base
    staff_scope = permission_membership_scope_q(
        roles=roles,
        permission="intelligence:read",
        branch_field="branch_id",
        department_field="department_id",
        account_kinds={"staff"},
    )
    me = teacher_profile_for(request.user)
    teacher_scope = permission_membership_scopes(
        roles=roles,
        permission="intelligence:read",
        account_kinds={"teacher"},
    )
    own_scope = Q(pk=me.pk) if me is not None and teacher_scope else Q(pk__in=[])
    return base.filter(staff_scope | own_scope).distinct()


def _is_family(request: HttpRequest, student) -> bool:
    """Whether the exact signed-in role principal owns this family relation.

    The compatibility ``User`` bridge may back staff, teacher, parent, and
    student accounts simultaneously. It is not an authorization identity.
    """
    kind = str(getattr(request, "principal_kind", "") or "")
    if kind not in {"", "student", "parent"}:
        return False
    try:
        principal = request_role_principal(
            request,
            allowed_kinds={"student", "parent"},
        )
    except PermissionException:
        return False
    if principal.kind == "student":
        return principal.principal_id == student.pk
    from apps.parents.models import Guardian

    return Guardian.objects.filter(
        student=student,
        parent_id=principal.principal_id,
        parent__user_id=principal.user_id,
        revoked_at__isnull=True,
    ).exists()


def _scoped_risk_students(request: HttpRequest):
    """Student scope for named risk data.

    General staff readers stay branch/department scoped through ``scoped_students``.
    A teacher without a management membership is narrower still: only cohorts they
    actually teach through a typed assignment, legacy primary assignment, or lesson.
    """
    from apps.cohorts.selectors import taught_cohorts

    roles = get_user_roles(request)
    qs = StudentProfile.objects.select_related("user", "branch", "current_cohort")
    if is_permission_unscoped(request, permission="intelligence:read"):
        return qs
    visible = permission_membership_scope_q(
        roles=roles,
        permission="intelligence:read",
        branch_field="branch_id",
        department_field="current_cohort__department_id",
        account_kinds={"staff"},
    )
    teacher_scope = permission_membership_scopes(
        roles=roles,
        permission="intelligence:read",
        account_kinds={"teacher"},
    )
    if teacher_scope:
        # Current named-risk access follows live cohort assignments only. A
        # completed historical lesson is legitimate delivery evidence elsewhere,
        # but must not grant a former teacher permanent access to today's roster.
        visible |= Q(
            current_cohort__in=taught_cohorts(
                user=request.user,
                include_lesson_teacher=False,
            )
        )
    return qs.filter(visible).distinct()


@openapi_contract(
    path="/api/v1/intelligence/executive-summary/",
    operations=(EXECUTIVE_GET_CONTRACT, EXECUTIVE_HEAD_CONTRACT),
)
@csrf_exempt
@require_auth
def executive_summary_view(request: HttpRequest) -> HttpResponse:
    """One permission-pruned snapshot for a leadership workspace.

    Scope filters are resolved against the exact staff membership that grants
    ``intelligence:read``.  Each optional domain section is then included only
    when its own read permission covers that entire selected scope.
    """

    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    branch_id, department_id, window = parse_executive_query(request)
    scope = resolve_executive_scope(
        request,
        branch_id=branch_id,
        department_id=department_id,
    )
    principal = request_role_principal(
        request,
        allowed_kinds={"staff"},
        error_code="management_principal_unavailable",
    )
    included_sections = included_executive_sections(request, scope)
    center_settings = get_center_settings()
    requested_locale = translation.get_language()
    if not request.headers.get("Accept-Language") and center_settings.default_language:
        requested_locale = center_settings.default_language
    locale = (requested_locale or settings.LANGUAGE_CODE).replace("_", "-").lower()
    # Finance storage is still explicitly Decimal-major ``*_uzs``.  Do not
    # relabel those values with a configurable presentation currency until the
    # versioned multi-currency migration supplies converted amounts.
    currency = "UZS"
    key = executive_cache_key(
        request,
        scope=scope,
        window=window,
        included_sections=included_sections,
        locale=locale,
        currency=currency,
        user_id=principal.user_id,
        principal_kind=principal.kind,
        principal_id=principal.principal_id,
    )

    def load_summary() -> dict[str, Any]:
        return _service().executive_summary(
            context=ExecutiveSummaryContext(
                generated_at=timezone.now(),
                scope=scope,
                window=window,
                locale=locale,
                currency=currency,
                included_sections=included_sections,
                user_id=principal.user_id,
                principal_kind=principal.kind,
                principal_id=principal.principal_id,
            )
        )

    cached = get_or_compute(
        backend=cache,
        key=key,
        policy=_executive_cache_policy(),
        loader=load_summary,
        logger=logger,
    )
    return _executive_response(request, cached)


@csrf_exempt
@require_auth
def risk_list_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    _validate_query(request, allowed={"cohort", "page", "page_size"})
    qs = _scoped_risk_students(request)
    cohort_id = positive_int_filter(request, "cohort")
    if cohort_id is not None:
        qs = qs.filter(current_cohort_id=cohort_id)
    page, page_size = _page_values(request)
    policy = _risk_cache_policy()
    include_finance = _can_see_finance(request)
    if not policy.enabled:
        return success(
            _service().risk_list(
                students=qs,
                include_finance=include_finance,
                page=page,
                page_size=page_size,
            )
        )
    principal = request_role_principal(
        request,
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        error_code="intelligence_principal_unavailable",
    )
    # Cache only organization-wide STAFF reads. Teacher cohort assignments and
    # branch/department student placement are mutable row-scope inputs that are
    # intentionally absent from the flat permission context; caching either can
    # expose a former student until the stale horizon expires.
    if principal.kind != "staff" or not is_permission_unscoped(
        request,
        permission="intelligence:read",
        account_kinds={"staff"},
    ):
        return success(
            _service().risk_list(
                students=qs,
                include_finance=include_finance,
                page=page,
                page_size=page_size,
            )
        )
    key = intelligence_cache_key(
        request,
        namespace="risk-list",
        principal=principal,
        scope={
            "resource": "student-risk",
            "finance_signal": include_finance,
        },
        query={
            "cohort": cohort_id,
            "page": page,
            "page_size": page_size,
            "as_of_date": timezone.localdate().isoformat(),
            "timezone": timezone.get_current_timezone_name(),
            "locale": translation.get_language() or settings.LANGUAGE_CODE,
        },
    )
    cached = get_or_compute(
        backend=cache,
        key=key,
        policy=policy,
        loader=lambda: _service().risk_list(
            students=qs,
            include_finance=include_finance,
            page=page,
            page_size=page_size,
        ),
        logger=logger,
    )
    return _private_cached_response(cached)


@csrf_exempt
@require_auth
def risk_detail_view(request: HttpRequest, student_id: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    _validate_query(request, allowed=set())
    student = _scoped_risk_students(request).filter(pk=student_id).first()
    if student is None:
        raise NotFoundException(_("Student not found."), code="not_found")
    policy = _risk_cache_policy()
    include_finance = _can_see_finance(request)
    if not policy.enabled:
        return success(
            _service().risk_detail(
                student=student,
                include_finance=include_finance,
            )
        )
    principal = request_role_principal(
        request,
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        error_code="intelligence_principal_unavailable",
    )
    key = intelligence_cache_key(
        request,
        namespace="risk-detail",
        principal=principal,
        scope={
            "resource": "student-risk",
            "finance_signal": include_finance,
        },
        query={
            "student": student_id,
            "as_of_date": timezone.localdate().isoformat(),
            "timezone": timezone.get_current_timezone_name(),
            "locale": translation.get_language() or settings.LANGUAGE_CODE,
        },
    )
    cached = get_or_compute(
        backend=cache,
        key=key,
        policy=policy,
        loader=lambda: _service().risk_detail(
            student=student,
            include_finance=include_finance,
        ),
        logger=logger,
    )
    return _private_cached_response(cached)


@csrf_exempt
@require_auth
def branch_ranking_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    _validate_query(request, allowed={"page", "page_size"})
    return success(
        _page_results(
            request,
            _service().branch_ranking(
                branches=_scoped_branches(request), include_finance=_can_see_finance(request)
            ),
        )
    )


@csrf_exempt
@require_auth
def family_health_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    _validate_query(request, allowed={"page", "page_size"})
    # Naming a family + surfacing children's risk needs parents:read at the
    # same branch-wide scope. An unrelated grant must not make an empty/denied
    # result look like a legitimate zero-family register.
    branch_ids = _branch_wide_permission_intersection(
        request,
        "intelligence:read",
        "parents:read",
    )
    if branch_ids == set():
        raise PermissionException(
            _("Family health needs visibility of family records."), code="not_permitted"
        )
    visible_branches = Branch.objects.filter(archived_at__isnull=True)
    if branch_ids is not None:
        visible_branches = visible_branches.filter(pk__in=branch_ids)
    return success(
        _page_results(
            request,
            _service().family_health(branches=visible_branches, include_finance=_can_see_finance(request)),
        )
    )


@csrf_exempt
@require_auth
def student_journey_view(request: HttpRequest, student_id: int) -> HttpResponse:
    """Family-facing timeline: the student + their guardians see their own; a STAFF
    caller must actually hold students:read (so e.g. IT, walled off academic data
    everywhere else, can't read it). Invoices need finance:read or being the family."""
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    _validate_query(request, allowed=set())
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    student = scoped_students(user=request.user, roles=roles).filter(pk=student_id).first()
    if student is None:
        raise NotFoundException(_("Student not found."), code="not_found")
    is_family = _is_family(request, student)
    cohort = student.current_cohort
    department_id = cohort.department_id if cohort is not None else None
    can_read_student = request_permission_membership_allows(
        request,
        permission="students:read",
        branch_id=student.branch_id,
        department_id=department_id,
        account_kinds={"staff", "teacher"},
    )
    # Out-of-scope callers receive 404 so this endpoint cannot confirm a student id.
    if not (request.user.is_superuser or is_family or can_read_student):
        raise NotFoundException(_("Student not found."), code="not_found")
    include_finance = (
        request.user.is_superuser
        or is_family
        or request_permission_membership_allows(
            request,
            permission="finance:read",
            branch_id=student.branch_id,
            department_id=department_id,
            account_kinds={"staff", "teacher"},
        )
    )
    return success(_service().student_journey(student=student, include_finance=include_finance))


@csrf_exempt
@require_auth
def rules_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    _validate_query(request, allowed=set())
    return success(_service().rules())


@csrf_exempt
@require_auth
def teacher_engagement_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    _validate_query(request, allowed={"page", "page_size"})
    page, page_size = _page_values(request)
    return success(
        _service().teacher_engagement(
            teachers=_scoped_teachers(request),
            page=page,
            page_size=page_size,
        )
    )
