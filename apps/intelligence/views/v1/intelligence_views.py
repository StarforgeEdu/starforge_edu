"""Intelligence HTTP views (layered, off DRF).

Seven read-only A-3 facets (transparent rules, no black box): dropout-risk list +
detail, branch ranking, family-health retention feed, a student's journey timeline,
the risk rules, and teacher engagement. All are GET. Every facet is scoped in the
view (which students/branches/teachers the caller may see) and rendered from the
preserved apps.intelligence.selectors read layer via IIntelligenceService.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.intelligence.interfaces.services import IIntelligenceService
from apps.org.models import Branch
from apps.students.selectors import scoped_students
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.permissions import (
    Role,
    _request_overrides,
    get_role_memberships,
    get_user_roles,
    has_permission_code,
)
from core.responses import error, success

# Only STAFF memberships grant a branch scope for the intelligence facets — a
# student/parent membership must never (e.g. via an A-2 grant of intelligence:read)
# resolve to a branch and open the branch-level feeds. This fails closed for them.
_STAFF_ROLES = frozenset(r for r in Role.ALL if r not in (Role.STUDENT, Role.PARENT))


def _service() -> IIntelligenceService:
    return container.resolve(IIntelligenceService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _can_see_finance(request: HttpRequest) -> bool:
    """Whether to include the overdue-payment flag — only callers who may see
    finance (finance:read / superuser) get the financial signal."""
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    return req.user.is_superuser or has_permission_code(
        get_user_roles(req), "finance:read", _request_overrides(req)
    )


def _scoped_branches(request: HttpRequest):
    """Branches the caller may rank: the director/superuser sees every (live) branch,
    a branch-scoped STAFF role sees only the branch(es) they belong to, non-staff none."""
    qs = Branch.objects.filter(archived_at__isnull=True)
    if request.user.is_superuser or Role.DIRECTOR in get_user_roles(request):
        return qs
    my = {m.branch_id for m in get_role_memberships(request) if m.branch_id and m.role in _STAFF_ROLES}
    return qs.filter(id__in=my)


def _scoped_teachers(request: HttpRequest):
    """Teachers whose engagement the caller may see: director/superuser → all; a
    manager (HOD) → their branch(es)' teachers; a teacher → only their own row;
    anyone else → none (fail closed, even with an A-2 intelligence:read grant)."""
    from apps.teachers.models import TeacherProfile
    from apps.teachers.selectors import teacher_profile_for

    base = TeacherProfile.objects.select_related("user")
    roles = get_user_roles(request)
    if request.user.is_superuser or Role.DIRECTOR in roles:
        return base
    if Role.HEAD_OF_DEPT in roles:
        my = {m.branch_id for m in get_role_memberships(request) if m.branch_id and m.role in _STAFF_ROLES}
        return base.filter(branch_id__in=my)
    me = teacher_profile_for(request.user)
    return base.filter(pk=me.pk) if me is not None else base.none()


def _is_family(request: HttpRequest, student) -> bool:
    """The student themselves, or one of their guardians."""
    user: Any = request.user
    if student.user_id == user.id:
        return True
    from apps.parents.models import Guardian

    return Guardian.objects.filter(student=student, parent__user=user).exists()


@csrf_exempt
@require_auth
def risk_list_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    qs = scoped_students(user=request.user, roles=get_user_roles(request)).select_related("user")
    cohort = request.GET.get("cohort")
    if cohort:
        try:
            cohort_id = int(cohort)
        except ValueError:
            raise ValidationException(
                "Invalid query parameter.",
                code="invalid_query_param",
                fields={"cohort": ["Must be an integer."]},
            ) from None
        qs = qs.filter(current_cohort_id=cohort_id)
    return success(_service().risk_list(students=qs, include_finance=_can_see_finance(request)))


@csrf_exempt
@require_auth
def risk_detail_view(request: HttpRequest, student_id: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    student = (
        scoped_students(user=request.user, roles=get_user_roles(request))
        .select_related("user")
        .filter(pk=student_id)
        .first()
    )
    if student is None:
        raise NotFoundException(_("Student not found."), code="not_found")
    return success(_service().risk_detail(student=student, include_finance=_can_see_finance(request)))


@csrf_exempt
@require_auth
def branch_ranking_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    return success(
        _service().branch_ranking(
            branches=_scoped_branches(request), include_finance=_can_see_finance(request)
        )
    )


@csrf_exempt
@require_auth
def family_health_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    # Naming a family + surfacing their children's risk needs family-record
    # visibility — gate out teachers (intelligence:read but no parents:read).
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    if not (
        req.user.is_superuser
        or has_permission_code(get_user_roles(req), "parents:read", _request_overrides(req))
    ):
        raise PermissionException(
            _("Family health needs visibility of family records."), code="not_permitted"
        )
    return success(
        _service().family_health(
            branches=_scoped_branches(request), include_finance=_can_see_finance(request)
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
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    roles = get_user_roles(req)
    student = scoped_students(user=request.user, roles=roles).filter(pk=student_id).first()
    if student is None:
        raise NotFoundException(_("Student not found."), code="not_found")
    overrides = _request_overrides(req)
    is_family = _is_family(request, student)
    # scoped_students alone would admit any STAFF_ROLES member (incl. IT) — require the
    # real read perm for staff; an out-of-scope caller gets 404 (no existence leak).
    if not (request.user.is_superuser or is_family or has_permission_code(roles, "students:read", overrides)):
        raise NotFoundException(_("Student not found."), code="not_found")
    include_finance = (
        request.user.is_superuser or is_family or has_permission_code(roles, "finance:read", overrides)
    )
    return success(_service().student_journey(student=student, include_finance=include_finance))


@csrf_exempt
@require_auth
def rules_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    return success(_service().rules())


@csrf_exempt
@require_auth
def teacher_engagement_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "intelligence:read")
    return success(_service().teacher_engagement(teachers=_scoped_teachers(request)))
