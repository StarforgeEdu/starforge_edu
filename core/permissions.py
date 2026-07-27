"""Role / permission matrix and DRF permission classes.

Single source of truth: ROLE_PERMISSION_MATRIX maps role -> set of action codes
(`'<resource>:<verb>'`).

TD-4 — fail-closed: a view that declares neither `required_perms[action]` nor a
`resource` from which to derive one is **denied** (never silently allowed).
TD-5 — per-action: views declare `resource = "<name>"` (CRUD verbs derived via
`default_perms`) plus `required_perms = {"<custom_action>": "<resource>:<verb>"}`
for every `@action`.
TD-13 — the active RoleMemberships are fetched once per request and memoized on
`request._role_memberships_cache`, so RolePermission + ObjectScopedPermission
never issue more than one membership query.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.http import HttpRequest
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

# Both request styles flow through the role/permission helpers now: DRF views pass
# a DRF Request, the layered plain views pass a Django HttpRequest. Both expose the
# `.user` (and cache attrs) these helpers read.
AnyRequest = HttpRequest | Request


class Role:
    DIRECTOR = "director"
    HEAD_OF_DEPT = "head_of_dept"
    TEACHER = "teacher"
    STUDENT = "student"
    PARENT = "parent"
    ACCOUNTANT = "accountant"
    CASHIER = "cashier"
    LIBRARIAN = "librarian"
    SECURITY = "security"
    IT = "it"
    REGISTRAR = "registrar"
    SUPPORT = "support"

    ALL = (
        DIRECTOR,
        HEAD_OF_DEPT,
        TEACHER,
        STUDENT,
        PARENT,
        ACCOUNTANT,
        CASHIER,
        LIBRARIAN,
        SECURITY,
        IT,
        REGISTRAR,
        SUPPORT,
    )


# Matrix — director sees everything; others see their own resource group. Lanes
# append real per-feature codes as each domain lands (additive edits only).
ROLE_PERMISSION_MATRIX: dict[str, set[str]] = {
    Role.DIRECTOR: {"*:*"},
    Role.HEAD_OF_DEPT: {
        "users:read",
        "students:*",
        "teachers:read",
        "cohorts:*",
        "attendance:*",
        "academics:*",
        "assignments:*",
        "schedule:*",
        "reports:read",
        "reports:write",  # D4-LB-5
        # F4-5: HOD is the manager-leg approver for content publication (reads
        # the library, gives the second sign-off, can also sign the teacher leg).
        "content:read",
        "content:approve",
        "content:publish",
        "audit:read",
        # D4-LA-8: AI request log + budget snapshot + exam generation (read+write).
        # ai:manage (budget edits) stays director-only via *:*.
        "ai:read",
        "ai:write",
        "printing:read",  # D4-LD-7
        "printing:write",
        # A-1: HOD is a manager-level approver (request + approve, not disburse).
        "approvals:read",
        "approvals:write",
        "approvals:approve",
        # #12: HOD can author + see the rule book.
        "compliance:read",
        "compliance:write",
        # F3-3: managers build + analyze forms/surveys.
        "forms:read",
        "forms:write",
        # F1-2/F1-4: HOD builds placement tests AND is the approver (maker-checker
        # blocks approving one's own; placement:approve stays manager-level).
        "placement:read",
        "placement:write",
        "placement:approve",
        # F5: HOD manages tasks + outranks within the dept (assign_any bypass).
        "tasks:read",
        "tasks:write",
        "tasks:assign_any",
        # F4-4: messaging.
        "messaging:read",
        "messaging:write",
        # A-3: HOD sees the risk-flag feed for their students.
        "intelligence:read",
        # F15-2: HOD manages achievements incl. approving teacher global requests.
        "achievements:read",
        "achievements:write",
        "achievements:approve",
        # F17-1: HOD defines reward types + grants them to staff.
        "rewards:read",
        "rewards:write",
        # F18-1: HOD assigns/opens/rejects cover requests.
        "cover:read",
        "cover:write",
        "cover:approve",
        # F21-1: HOD raises + tracks staff loans.
        "loan:read",
        "loan:write",
        # #15: HOD raises + tracks purchase orders.
        "procurement:read",
        "procurement:write",
        # F10-1: HOD runs SMS campaigns to their students' families.
        "campaign:read",
        "campaign:write",
        "campaign:send",
        # F24-1: HOD issues + reverses student demerits, and disciplines STAFF
        # (penalty:staff — a management action a peer teacher does not hold).
        "penalty:read",
        "penalty:write",
        "penalty:waive",
        "penalty:staff",
        # F3-5: HOD schedules staff meetings (reading/RSVP is open to invitees).
        "meeting:write",
        # F12-1: HOD manages card types + issues/revokes cards in their branch, and reads
        # student wallets (oversight; the cashier/reception load + charge them).
        "card:read",
        "card:write",
        "wallet:read",
    },
    Role.TEACHER: {
        "students:read",
        "cohorts:read",
        # D1-LB-3 / D1-LF-8 acceptance: teachers read org structure (branches,
        # rooms, working hours, settings knobs) — never write it.
        "org:read",
        "attendance:*",
        "academics:read",  # D2-C-7: pairs with academics:write (Day-1 asymmetry fix)
        "academics:write",
        "assignments:*",
        "schedule:read",
        "content:*",
        # D4-LA-8: teachers read the AI log + request exam generation (ai:write).
        "ai:read",
        "ai:write",
        "reports:read",  # D4-LB-5: run own-cohort enrollment/attendance/grades reports
        "reports:write",
        "printing:read",  # D4-LD-7: request prints
        "printing:write",
        # A-1: teachers can raise requests (expense/loan/discount/salary-prep).
        "approvals:read",
        "approvals:write",
        # F3-3: teachers build surveys for their groups + fill manager forms.
        "forms:read",
        "forms:write",
        # F1-2: teachers (academic staff) author placement tests; a manager
        # approves them (no placement:approve here = enforced maker-checker).
        "placement:read",
        "placement:write",
        # F5: teachers task their assistants/lower grades + see their own tasks.
        "tasks:read",
        "tasks:write",
        # F4-4: teachers message students/parents.
        "messaging:read",
        "messaging:write",
        # A-3: teachers see at-risk students in their groups.
        "intelligence:read",
        # F15-2: teachers create group achievements + grant; request globals.
        "achievements:read",
        "achievements:write",
        "rewards:read",  # F17-1: teachers see rewards they received
        # F18-1: teachers request cover for their lessons + claim pooled ones.
        "cover:read",
        "cover:write",
        # F21-1: teachers can request a staff loan + see their own.
        "loan:read",
        "loan:write",
        # F24-1: teachers issue demerits to students (managers waive).
        "penalty:read",
        "penalty:write",
    },
    Role.STUDENT: {
        # students:read is row-scoped to self by apps/students/selectors.py
        # (read_self semantics live in selectors, not the gate — TD-5).
        "students:read",
        "schedule:read",
        "attendance:read",  # row-scoped to self in apps/attendance/selectors.py
        "academics:read",  # row-scoped to self + publication gate in apps/academics/selectors.py
        "assignments:read",
        "assignments:submit",  # D2-D-6: students submit their own work
        "content:read",
        "forms:read",  # F3-3: students fill surveys/forms addressed to them
        # F4-4: a student messages their teachers (the service blocks student↔student).
        "messaging:read",
        "messaging:write",
        # F15-2: a student sees their own wall of achievements.
        "achievements:read",
        # F24-1: a student sees their own demerit record (row-scoped to self).
        "penalty:read",
        # F12-1: a student sees their own card(s) (row-scoped to self). Their own wallet
        # is read via /wallets/me/ (IsAuthenticated, self-resolved) — NOT wallet:read,
        # which is the STAFF grant for reading any student's wallet by id.
        "card:read",
    },
    Role.PARENT: {
        # Row-scoped by selectors: students -> guardian-linked children only,
        # parents -> own profile only (TD-5 read_own_children semantics).
        "students:read",
        "parents:read",
        "students:read_own_children",
        "attendance:read",  # row-scoped to guardian-linked children in selectors
        "academics:read",  # row-scoped to children + publication gate in selectors
        "content:read",  # row-scoped to children's cohorts via apps/content/selectors._related_cohort_ids
        "finance:read_own",
        "schedule:read",
        "notifications:read",
        "forms:read",  # F3-3: parents fill forms addressed to families
        # F4-4: parents message staff (the service blocks parent↔student/parent).
        "messaging:read",
        "messaging:write",
        # F15-2: parents see their children's achievements.
        "achievements:read",
        # F24-1: parents see their children's demerit record.
        "penalty:read",
    },
    Role.ACCOUNTANT: {
        "finance:*",
        "payments:*",
        "reports:read",
        "reports:write",
        # A-1: accountant requests, approves, disburses, and reads the ledger.
        "approvals:read",
        "approvals:write",
        "approvals:approve",
        "approvals:disburse",
        "ledger:read",
        "tasks:read",  # F5: assignable + tracks own tasks
        "rewards:read",  # F17-1: receives + sees own rewards
        # F21-1: accountant raises, tracks, and collects repayments on loans.
        "loan:read",
        "loan:write",
        "loan:collect",
        # #15: accountant raises + reviews purchase orders.
        "procurement:read",
        "procurement:write",
        # #8: accountant sees + can refund book/material sales.
        "sale:read",
        "sale:write",
        "sale:refund",
        "penalty:read",  # F24-1: see own discipline
    },
    # A-1: the cashier disburses approved requests + reads the ledger (the till).
    Role.CASHIER: {
        "finance:read",
        "payments:write",
        "approvals:read",
        "approvals:disburse",
        "ledger:read",
        "tasks:read",
        "rewards:read",
        # F21-1: the cashier collects loan repayments (money into the till).
        "loan:read",
        "loan:collect",
        # #15: the cashier sees the POs they pay out.
        "procurement:read",
        # #8: the cashier rings up book/material sales + refunds (the till).
        "sale:read",
        "sale:write",
        "sale:refund",
        "penalty:read",  # F24-1: see own discipline
        # F12-1: the cashier loads + charges student wallets (stored value at the till).
        "wallet:read",
        "wallet:write",
    },
    # F24-1: every staff member holds penalty:read so they can see their OWN disciplinary
    # record (get_queryset still scopes a non-manager to their own rows only).
    Role.LIBRARIAN: {"content:*", "students:read", "cohorts:read", "tasks:read", "rewards:read", "penalty:read"},
    # F12-1: security scans cards at the door + reads them; reception (REGISTRAR) issues.
    Role.SECURITY: {
        "attendance:write", "users:read", "tasks:read", "rewards:read", "penalty:read",
        "card:read", "card:scan",
    },
    Role.IT: {
        "users:read",
        "audit:read",
        "org:*",
        "compliance:read",
        "compliance:write",
        "tasks:read",
        "rewards:read",
        "penalty:read",  # F24-1: see own discipline
    },
    Role.REGISTRAR: {
        "students:*",
        "users:write",
        "cohorts:*",
        "parents:*",
        "teachers:read",
        # F12-1: reception issues cards + scans them at the front desk, and loads/charges
        # student wallets (stored value at the front desk).
        "card:read",
        "card:write",
        "card:scan",
        "wallet:read",
        "wallet:write",
        "schedule:*",
        "printing:read",  # D4-LD-7: manage printers/agents
        "printing:write",
        # A-1: reception can raise requests too.
        "approvals:read",
        "approvals:write",
        # F3-3: reception builds + fills forms.
        "forms:read",
        "forms:write",
        # F1-2/F1-5: reception builds placement tests + (later) assigns them to
        # leads; approval is a manager's job (no placement:approve here).
        "placement:read",
        "placement:write",
        # F5: reception creates + tracks tasks.
        "tasks:read",
        "tasks:write",
        # F4-4: reception messages students/parents.
        "messaging:read",
        "messaging:write",
        # A-3: reception sees the at-risk feed (retention follow-up).
        "intelligence:read",
        # F15-2: reception manages + grants achievements.
        "achievements:read",
        "achievements:write",
        "rewards:read",  # F17-1
        # F18-1: reception coordinates cover (assign/open/reject).
        "cover:read",
        "cover:write",
        "cover:approve",
        # F21-1: reception raises + tracks staff loans.
        "loan:read",
        "loan:write",
        # #15: reception raises purchase orders (supplies).
        "procurement:read",
        "procurement:write",
        # F10-1: reception runs SMS campaigns (the core outreach desk).
        "campaign:read",
        "campaign:write",
        "campaign:send",
        # F24-1: reception issues + reverses student demerits.
        "penalty:read",
        "penalty:write",
        "penalty:waive",
        # #8: reception rings up book/material sales (refunds stay with finance).
        "sale:read",
        "sale:write",
        # F3-5: reception schedules staff meetings.
        "meeting:write",
    },
    Role.SUPPORT: {"users:read", "audit:read", "tasks:read", "rewards:read", "penalty:read"},
}


DEFAULT_VERB_FOR_ACTION: dict[str, str] = {
    "list": "read",
    "retrieve": "read",
    "create": "write",
    "update": "write",
    "partial_update": "write",
    "destroy": "write",
}


def default_perms(resource: str) -> dict[str, str]:
    """Standard CRUD permission map for a resource (TD-5).

    Spread into a viewset's `required_perms` and add custom `@action` codes:
    `required_perms = {**default_perms("students"), "transition": "students:write"}`.
    """
    return {action: f"{resource}:{verb}" for action, verb in DEFAULT_VERB_FOR_ACTION.items()}


def _load_tenant_overrides() -> dict[str, dict[str, str]]:
    """`{role: {permission: effect}}` for the active tenant (A-2). One small query
    over the (tiny) override table. Empty on the public schema (the table is
    tenant-only) or if it is not yet migrated, so the static matrix always governs
    as a safe fallback. Loaded once per request (memoized on the request by the
    permission classes) — there is no cross-request cache, so a grant/revoke takes
    effect on the very next request with no staleness window."""
    from django_tenants.utils import get_public_schema_name

    from core.utils import current_schema

    if current_schema() == get_public_schema_name():
        return {}
    # The override table is in TENANT_APPS, so it exists in every migrated tenant
    # schema — the only way it is absent is pre-migration, which never coincides
    # with request-flow permission checks. So we read it directly (one cheap SELECT,
    # no per-request savepoint overhead); a genuinely missing table would surface
    # loudly as a setup error rather than being silently swallowed.
    out: dict[str, dict[str, str]] = {}
    from apps.access.models import RolePermissionOverride

    for ov in RolePermissionOverride.objects.all().only("role", "permission", "effect"):
        out.setdefault(ov.role, {})[ov.permission] = ov.effect
    return out


def _request_overrides(request: AnyRequest) -> dict[str, dict[str, str]]:
    """The override map, fetched once per request and memoized (mirrors
    get_role_memberships) so multiple permission checks share a single query."""
    cached = getattr(request, "_perm_overrides_cache", None)
    if cached is None:
        cached = _load_tenant_overrides()
        request._perm_overrides_cache = cached  # type: ignore[union-attr]
    return cached


def _role_grant_revoke(role: str, overrides: dict[str, dict[str, str]]) -> tuple[set[str], set[str]]:
    """`(granted, revoked)` permission-code sets for `role`: the static matrix plus
    this tenant's grant overrides, and the revoke overrides kept separate (they are
    applied at match time so they can override a resource-wildcard grant)."""
    granted = set(ROLE_PERMISSION_MATRIX.get(role, set()))
    revoked: set[str] = set()
    for permission, effect in overrides.get(role, {}).items():
        (granted if effect == "grant" else revoked).add(permission)
    return granted, revoked


def _code_allowed(granted: set[str], revoked: set[str], code: str) -> bool:
    """Does `(granted, revoked)` authorize `code`?

    The master wildcard `*:*` is absolute and revoke-immune (a director keeping it
    can never be locked out). Otherwise a revoke — exact OR the covering
    resource-wildcard — denies the code even when a resource-wildcard grant would
    cover it, so a center can genuinely carve a verb out of a wildcard role. A grant
    then allows via exact code or the resource-wildcard.
    """
    if "*:*" in granted:
        return True
    resource, _, _verb = code.partition(":")
    if code in revoked or f"{resource}:*" in revoked:
        return False
    return f"{resource}:*" in granted or code in granted


def role_effective_permissions(
    role: str, overrides: dict[str, dict[str, str]] | None = None
) -> dict[str, list[str]]:
    """`{"granted": [...], "revoked": [...]}` for `role` with this tenant's overrides
    applied — the honest representation for the admin UI (a revoke can scope a verb
    out of a wildcard grant, which a single flat set could not express)."""
    if overrides is None:
        overrides = _load_tenant_overrides()
    granted, revoked = _role_grant_revoke(role, overrides)
    return {"granted": sorted(granted), "revoked": sorted(revoked)}


def roles_with_permission(code: str, overrides: dict[str, dict[str, str]] | None = None) -> set[str]:
    """Every role whose EFFECTIVE permissions authorize `code` (overrides included).
    Used to find notification recipients for a permission (e.g. who can disburse)."""
    if overrides is None:
        overrides = _load_tenant_overrides()
    out: set[str] = set()
    for role in ROLE_PERMISSION_MATRIX:
        granted, revoked = _role_grant_revoke(role, overrides)
        if _code_allowed(granted, revoked, code):
            out.add(role)
    return out


def has_permission_code(
    roles: Iterable[str], code: str, overrides: dict[str, dict[str, str]] | None = None
) -> bool:
    if overrides is None:
        overrides = _load_tenant_overrides()
    for role in roles:
        granted, revoked = _role_grant_revoke(role, overrides)
        if _code_allowed(granted, revoked, code):
            return True
    return False


def get_role_memberships(request: AnyRequest) -> list:
    """Active RoleMemberships for the request user, fetched once and memoized."""
    cached = getattr(request, "_role_memberships_cache", None)
    if cached is not None:
        return cached
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        memberships: list = []
    else:
        memberships = list(user.role_memberships.filter(revoked_at__isnull=True))
    request._role_memberships_cache = memberships  # type: ignore[union-attr]
    return memberships


def get_user_roles(request: AnyRequest) -> set[str]:
    return {m.role for m in get_role_memberships(request)}


class RolePermission(BasePermission):
    """TD-5 per-action; TD-4 fail-closed: no declaration => deny."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        action = getattr(view, "action", None) or (request.method or "").lower()
        required = (getattr(view, "required_perms", None) or {}).get(action)
        if required is None:
            resource = getattr(view, "resource", None)
            verb = DEFAULT_VERB_FOR_ACTION.get(action)
            if resource is None or verb is None:
                return False  # TD-4: deny, never fall through to permissive default
            required = f"{resource}:{verb}"
        return has_permission_code(get_user_roles(request), required, _request_overrides(request))


class ObjectScopedPermission(BasePermission):
    """Object-level scoping by branch/department.

    Views set `object_scope = "branch" | "department"`; the object exposes
    `branch_id` and/or `department_id`. Director and superuser bypass.
    """

    def has_object_permission(self, request: Request, view: APIView, obj: object) -> bool:
        user = request.user
        if user.is_superuser:
            return True
        memberships = get_role_memberships(request)
        if any(m.role == Role.DIRECTOR for m in memberships):
            return True
        scope = getattr(view, "object_scope", None)
        if scope is None:
            return True
        if scope == "branch":
            allowed = {m.branch_id for m in memberships}
            return getattr(obj, "branch_id", None) in allowed
        if scope == "department":
            allowed = {m.department_id for m in memberships if m.department_id}
            return getattr(obj, "department_id", None) in allowed
        return False


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def is_read_only_token(request: Request) -> bool:
    """True when the request is authenticated by a read-only impersonation token
    (access claim ``read_only=true``). Surfaced by the JWT auth class as
    ``request.is_read_only_token``; falls back to the raw ``request.auth`` claim."""
    read_only = bool(getattr(request, "is_read_only_token", False))
    if not read_only:
        auth = getattr(request, "auth", None)
        try:
            read_only = bool(auth.get("read_only")) if auth is not None else False
        except AttributeError:
            read_only = False
    return read_only


class DenyWriteForReadOnlyToken(BasePermission):
    """D4-LE-4: a read-only impersonation token (claim ``read_only=true``) may make
    only SAFE (GET/HEAD/OPTIONS) requests. Any write → 403 ``read_only_token``.
    Normal tokens (no claim) are unaffected.

    NOTE: this only covers views that include it in ``permission_classes``. The
    authoritative, opt-out-proof enforcement lives in ``core.viewsets`` (both base
    classes call ``assert_not_read_only_write`` in ``initial``) so an APIView that
    overrides ``permission_classes`` can never silently regain write access."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        if request.method in SAFE_METHODS:
            return True
        if is_read_only_token(request):
            from core.exceptions import PermissionException

            raise PermissionException(code="read_only_token")
        return True
