"""Compliance (rule book + penalties) endpoints — plain Django views over the layered stack.

Rules: managers (compliance:write) author; ANY authenticated user reads + acknowledges the
rules that apply to them (mine/pending/acknowledge). Penalties: a teacher/manager
(penalty:write) issues a student demerit; a manager (penalty:staff) disciplines staff; a
manager (penalty:waive — a SEPARATE perm for SoD) reverses one. The subject (student/parent/
staff) reads their OWN record; staff read their branch's (a peer teacher never sees a
colleague's disciplinary record). No PUT/PATCH/DELETE on penalties (405).
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.compliance.interfaces.services import IPenaltyService, IRuleService
from apps.compliance.presenters import penalty_to_dict, rule_to_dict, rule_with_ack_to_dict
from core.api_auth import check_perm, deny_read_only_token, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import Role, get_role_memberships, get_user_roles, has_permission_code
from core.responses import created, error, no_content, paginated, success


def _rule_service() -> IRuleService:
    return container.resolve(IRuleService)  # type: ignore[type-abstract]


def _penalty_service() -> IPenaltyService:
    return container.resolve(IPenaltyService)  # type: ignore[type-abstract]


def _roles(request: HttpRequest):
    req: Any = request
    return get_user_roles(req)


# --- rules -----------------------------------------------------------------
@csrf_exempt
@require_auth
def rules_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "compliance:read")
        qs = apply_filters(
            request,
            _rule_service().list_rules(),
            filter_fields=("is_active",),
            search_fields=("title",),
            ordering_fields=("title",),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([rule_to_dict(r) for r in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "compliance:write")
        body = read_json(request)
        title = str_field(body, "title", max_length=200).strip()
        rule_body = str_field(body, "body").strip()
        if not title or not rule_body:
            raise ValidationException(
                "title and body are required.",
                code="validation_error",
                fields={"title": ["Required."], "body": ["Required."]},
            )
        rule = _rule_service().create(
            title=title,
            body=rule_body,
            applies_to_roles=_roles_list(body),
            is_active=bool_field(body, "is_active", default=True),
            created_by=request.user,
        )
        return created(rule_to_dict(rule))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def rule_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "compliance:read" if read else "compliance:write")
    rule = _rule_service().get(pk=pk)
    if rule is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(rule_to_dict(rule))
    if request.method in ("PUT", "PATCH"):
        return success(rule_to_dict(_rule_service().update(rule, _rule_changes(request))))
    if request.method == "DELETE":
        _rule_service().delete(rule)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def rule_mine_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    rules, acked = _rule_service().mine(user=request.user, roles=_roles(request))
    return success([rule_with_ack_to_dict(r, acked) for r in rules])


@csrf_exempt
@require_auth
def rule_pending_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    rules = _rule_service().pending(user=request.user, roles=_roles(request))
    return success([rule_to_dict(r) for r in rules])


@csrf_exempt
@require_auth
def rule_acknowledge_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    # acknowledge is a write with no perm code (any authed user), so reinstate the
    # read-only-token deny the old TenantSafeModelViewSet.initial() gave (else a read-only
    # impersonation session could forge a rule acknowledgment).
    deny_read_only_token(request)
    rule = _rule_service().get_active(pk=pk)
    if rule is None:
        raise NotFoundException(code="not_found")
    roles = set(_roles(request))
    targets = rule.applies_to_roles or []
    if targets and not getattr(request.user, "is_superuser", False) and not (set(targets) & roles):
        raise PermissionException(_("This rule does not apply to you."), code="rule_not_applicable")
    ack = _rule_service().acknowledge(rule=rule, user=request.user)
    return success({"acknowledged": True, "version": ack.version})


# --- penalties -------------------------------------------------------------
@csrf_exempt
@require_auth
def penalties_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "penalty:read")
        is_director, can_waive, can_write, branch_ids = _penalty_scope(request)
        qs = _penalty_service().scoped_list(
            is_director=is_director,
            user=request.user,
            branch_ids=branch_ids,
            can_waive=can_waive,
            can_write=can_write,
        )
        qs = apply_filters(
            request,
            qs,
            filter_fields=("status", "student", "staff", "branch"),
            ordering_fields=("issued_at", "points"),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([penalty_to_dict(p) for p in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "penalty:write")
        return _issue_penalty(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def penalty_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "penalty:read")
    is_director, can_waive, can_write, branch_ids = _penalty_scope(request)
    penalty = _penalty_service().get_visible(
        is_director=is_director,
        user=request.user,
        branch_ids=branch_ids,
        can_waive=can_waive,
        can_write=can_write,
        pk=pk,
    )
    if penalty is None:
        raise NotFoundException(code="not_found")
    return success(penalty_to_dict(penalty))


@csrf_exempt
@require_auth
def penalty_staff_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "penalty:staff")
    body = read_json(request)
    staff = _penalty_service().resolve_active_user(user_id=int_field(body, "staff", required=True))  # type: ignore[arg-type]
    if staff is None:
        raise ValidationException(
            "Unknown staff member.", code="validation_error", fields={"staff": ["No such active user."]}
        )
    branch = _penalty_service().resolve_branch(branch_id=int_field(body, "branch", required=True))  # type: ignore[arg-type]
    if branch is None:
        raise ValidationException(
            "Unknown or archived branch.", code="validation_error", fields={"branch": ["No such branch."]}
        )
    is_director, _cw, _cwr, branch_ids = _penalty_scope(request)
    if not is_director and branch.id not in branch_ids:
        raise PermissionException(
            _("You can only discipline staff in your own branch."), code="branch_out_of_scope"
        )
    penalty = _penalty_service().issue_staff(
        staff=staff,
        branch=branch,
        points=_points(body),
        reason=_reason(body),
        issued_by=request.user,
        rule=_optional_rule(request, body),
    )
    return created(penalty_to_dict(penalty))


@csrf_exempt
@require_auth
def penalty_waive_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, "penalty:waive")
    is_director, can_waive, can_write, branch_ids = _penalty_scope(request)
    penalty = _penalty_service().get_visible(
        is_director=is_director,
        user=request.user,
        branch_ids=branch_ids,
        can_waive=can_waive,
        can_write=can_write,
        pk=pk,
    )
    if penalty is None:
        raise NotFoundException(code="not_found")
    result = _penalty_service().waive(
        penalty, actor=request.user, reason=str_field(read_json(request), "reason", max_length=255)
    )
    return success(penalty_to_dict(result))


# --- helpers ---------------------------------------------------------------
def _penalty_scope(request: HttpRequest) -> tuple[bool, bool, bool, set[int]]:
    req: Any = request
    roles = get_user_roles(req)
    is_director = bool(getattr(request.user, "is_superuser", False)) or Role.DIRECTOR in roles
    can_waive = has_permission_code(roles, "penalty:waive")
    can_write = has_permission_code(roles, "penalty:write")
    branch_ids = {m.branch_id for m in get_role_memberships(req) if m.branch_id}
    return is_director, can_waive, can_write, branch_ids


def _issue_penalty(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    student = _penalty_service().resolve_student(student_id=int_field(body, "student", required=True))  # type: ignore[arg-type]
    if student is None:
        raise ValidationException(
            "Unknown student.", code="validation_error", fields={"student": ["No such student."]}
        )
    is_director, _cw, _cwr, branch_ids = _penalty_scope(request)
    if not is_director and student.branch_id not in branch_ids:
        raise PermissionException(
            _("You can only penalise a student in your own branch."), code="branch_out_of_scope"
        )
    penalty = _penalty_service().issue(
        student=student,
        points=_points(body),
        reason=_reason(body),
        issued_by=request.user,
        rule=_optional_rule(request, body),
    )
    return created(penalty_to_dict(penalty))


def _points(body: dict[str, Any]) -> int:
    points = int_field(body, "points", required=True)
    if points is None or points < 1:
        raise ValidationException(
            "points must be a positive integer.",
            code="validation_error",
            fields={"points": ["Must be an integer >= 1."]},
        )
    return points


def _reason(body: dict[str, Any]) -> str:
    reason = str_field(body, "reason", max_length=255)
    if not reason.strip():
        raise ValidationException(
            "reason is required.", code="validation_error", fields={"reason": ["This field is required."]}
        )
    return reason


def _optional_rule(request: HttpRequest, body: dict[str, Any]):
    if body.get("rule") is None:
        return None
    rule = _penalty_service().resolve_active_rule(rule_id=int_field(body, "rule", required=True))  # type: ignore[arg-type]
    if rule is None:  # unknown or retired -> can't be cited
        raise ValidationException(
            "A retired or unknown rule cannot be cited.",
            code="validation_error",
            fields={"rule": ["No such active rule."]},
        )
    return rule


def _rule_changes(request: HttpRequest) -> dict[str, Any]:
    body = read_json(request)
    changes: dict[str, Any] = {}
    if "title" in body:
        title = str_field(body, "title", max_length=200).strip()
        if not title:
            raise ValidationException(
                "title may not be blank.", code="validation_error", fields={"title": ["May not be blank."]}
            )
        changes["title"] = title
    if "body" in body:
        rule_body = str_field(body, "body").strip()
        if not rule_body:
            raise ValidationException(
                "body may not be blank.", code="validation_error", fields={"body": ["May not be blank."]}
            )
        changes["body"] = rule_body
    if "applies_to_roles" in body:
        changes["applies_to_roles"] = _roles_list(body)
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active")
    return changes


def _roles_list(body: dict[str, Any]) -> list:
    """applies_to_roles is a JSON list of role-code STRINGS. Each MUST be a string — a
    non-string element would break `set(targets) & roles` (unhashable -> TypeError 500 in
    acknowledge / the rule selectors)."""
    raw = body.get("applies_to_roles", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationException(
            "applies_to_roles must be a list.",
            code="validation_error",
            fields={"applies_to_roles": ["Must be a list of role codes."]},
        )
    for value in raw:
        if not isinstance(value, str):
            raise ValidationException(
                "Each role code must be a string.",
                code="validation_error",
                fields={"applies_to_roles": ["Each entry must be a string role code."]},
            )
    return raw
