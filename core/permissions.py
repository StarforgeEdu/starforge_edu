"""Role / permission matrix shared by layered and reports API views.

Single source of truth: ROLE_PERMISSION_MATRIX maps role -> set of action codes
(`'<resource>:<verb>'`).

TD-4 — fail-closed: a view that declares neither `required_perms[action]` nor a
`resource` from which to derive one is **denied** (never silently allowed).
TD-5 — the remaining reports viewsets declare a resource or explicit per-action
permission; layered views call ``core.api_auth.check_perm``.
TD-13 — the active RoleMemberships are fetched once per request and memoized on
`request._role_memberships_cache`, so repeated checks issue one membership query.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from django.db.models import Q
from django.http import HttpRequest
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

# Both request styles flow through the role/permission helpers now: DRF views pass
# a DRF Request, the layered plain views pass a Django HttpRequest. Both expose the
# `.user` (and cache attrs) these helpers read.
AnyRequest = HttpRequest | Request


@dataclass(frozen=True)
class MembershipGrantScope:
    branch_id: int
    department_id: int | None
    role: str
    account_kind: str
    grants: frozenset[str]
    is_legacy_fallback: bool = False
    is_organization_wide: bool = False


@dataclass(frozen=True)
class EffectivePermissionScope:
    """Effective grants at one branch/department boundary.

    Multiple account-type assignments can share the same boundary.  They are
    intentionally collapsed into one deterministic union because authorization
    also treats independently valid grants as additive at that boundary.
    """

    branch_id: int | None
    department_id: int | None
    permissions: tuple[str, ...]


class PermissionRoleSet(set[str]):
    """Legacy role names plus canonical grants loaded for their memberships.

    The set behavior preserves every existing row-scope check. Authorization
    additionally reads ``canonical_grants`` for linked active AccountTypes and
    consults the static matrix only for ``fallback_roles`` whose old membership
    has not yet been backfilled.
    """

    def __init__(
        self,
        roles: Iterable[str] = (),
        *,
        canonical_grants: Iterable[str] = (),
        fallback_roles: Iterable[str] = (),
        account_kinds: Iterable[str] = (),
        membership_scopes: Iterable[MembershipGrantScope] = (),
    ) -> None:
        super().__init__(roles)
        self.canonical_grants = frozenset(canonical_grants)
        self.fallback_roles = frozenset(fallback_roles)
        self.account_kinds = frozenset(account_kinds)
        self.membership_scopes = tuple(membership_scopes)


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
        # Organization structure is still intersected with the exact branch /
        # department membership supplying this grant.  A department head needs
        # those readable names and operating locations without receiving a
        # tenant-wide directory.
        "org:read",
        "users:read",
        "students:*",
        "crm:read",
        "crm:write",
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
        # Compensation is a separate privacy and authorization boundary from
        # both the faculty directory and ordinary customer finance.  A finance
        # grant alone must never disclose or mutate staff pay.
        "compensation:read",
        "compensation:write",
        "compensation:run",
        "compensation:approve",
        "compensation:disburse",
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
        # Cashiers may release an already approved salary, but cannot inspect
        # payout policies, change rates, prepare runs, or approve them.
        "compensation:disburse",
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
    Role.LIBRARIAN: {
        "content:*",
        "students:read",
        "cohorts:read",
        "tasks:read",
        "rewards:read",
        "penalty:read",
    },
    # F12-1: security scans cards at the door + reads them; reception (REGISTRAR) issues.
    Role.SECURITY: {
        "attendance:write",
        "users:read",
        "tasks:read",
        "rewards:read",
        "penalty:read",
        "card:read",
        "card:scan",
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
        "crm:read",
        "crm:write",
        # Safeguarding data is deliberately outside ``students:*``.  A normal
        # directory/editor grant must never imply access to encrypted medical
        # notes or authority to replace emergency-contact records.
        "safeguarding:read",
        "safeguarding:write",
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

# Every authenticated tenant account manages its own notification preferences.
# Keep this invariant additive for new named roles; the director already holds
# the master wildcard and does not need a redundant literal grant.
for _notification_role in Role.ALL:
    if _notification_role != Role.DIRECTOR:
        ROLE_PERMISSION_MATRIX[_notification_role].add("notifications:read")


DEFAULT_VERB_FOR_ACTION: dict[str, str] = {
    "list": "read",
    "retrieve": "read",
    "create": "write",
    "update": "write",
    "partial_update": "write",
    "destroy": "write",
}


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


def _flat_effective_grants(granted: set[str], revoked: set[str]) -> set[str]:
    """Return a flat, UI-safe representation of one grant/revoke set.

    A flat capability array cannot express ``students:*`` minus
    ``students:write``.  In that case the wildcard is expanded into the known
    concrete catalogue and the revoked verbs are removed.  This mirrors the
    canonical system-account synchronization and, critically, never reports a
    broader permission than :func:`has_permission_code` would authorize.
    """
    if "*:*" in granted:
        return {"*:*"}

    effective: set[str] = set()
    catalogue: set[str] | None = None
    for permission in granted:
        resource, _separator, verb = permission.partition(":")
        if verb == "*" and any(item.partition(":")[0] == resource for item in revoked):
            if catalogue is None:
                # Local import avoids the core.permissions -> access.validation
                # module cycle during application startup.
                from apps.access.validation import permission_catalogue

                catalogue = permission_catalogue()
            effective.update(
                code
                for code in catalogue
                if code.partition(":")[0] == resource
                and code.partition(":")[2] != "*"
                and _code_allowed(granted, revoked, code)
            )
        elif _code_allowed(granted, revoked, permission):
            effective.add(permission)
    return effective


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


def role_memberships_with_permission(code: str):
    """Active memberships that *individually* grant ``code``.

    Recipient discovery must not first resolve a user's aggregate permissions and
    then filter a different membership by branch: that lets a grant in Branch A
    borrow an unrelated membership in Branch B. Canonical AccountType grants and
    legacy/null fallbacks are therefore matched in the same queryset row.
    """
    from apps.users.models import RoleMembership

    resource, separator, _verb = code.partition(":")
    covering_grants = {code, "*:*"}
    if separator:
        covering_grants.add(f"{resource}:*")
    legacy_roles = roles_with_permission(code)
    return (
        RoleMembership.objects.filter(revoked_at__isnull=True, user__is_active=True)
        .filter(
            Q(
                account_type__is_active=True,
                account_type__permission_rows__permission__in=covering_grants,
            )
            | Q(account_type__isnull=True, role__in=legacy_roles)
        )
        .select_related("account_type")
        .distinct()
    )


def role_memberships_for_account_kinds(account_kinds: Iterable[str]):
    """Active principal memberships for canonical account kinds.

    The null-AccountType branch is migration compatibility only. New/custom
    account types are classified by ``account_kind`` rather than by the legacy
    compatibility role stored on RoleMembership.
    """
    from apps.access.models import AccountType
    from apps.users.models import RoleMembership

    kind_set = set(account_kinds)
    legacy_roles: set[str] = set()
    if AccountType.AccountKind.STAFF in kind_set:
        legacy_roles.update(
            role for role in Role.ALL if role not in (Role.TEACHER, Role.STUDENT, Role.PARENT)
        )
    if AccountType.AccountKind.TEACHER in kind_set:
        legacy_roles.add(Role.TEACHER)
    if AccountType.AccountKind.STUDENT in kind_set:
        legacy_roles.add(Role.STUDENT)
    if AccountType.AccountKind.PARENT in kind_set:
        legacy_roles.add(Role.PARENT)

    return (
        RoleMembership.objects.filter(revoked_at__isnull=True, user__is_active=True)
        .filter(
            Q(account_type__is_active=True, account_type__account_kind__in=kind_set)
            | Q(account_type__isnull=True, role__in=legacy_roles)
        )
        .select_related("account_type")
        .distinct()
    )


def has_permission_code(
    roles: Iterable[str], code: str, overrides: dict[str, dict[str, str]] | None = None
) -> bool:
    evaluated_roles: Iterable[str] = roles
    if isinstance(roles, PermissionRoleSet):
        if _code_allowed(set(roles.canonical_grants), set(), code):
            return True
        # AccountType permissions are canonical. The legacy matrix/override
        # resolver is consulted only for memberships not linked by migration or
        # deliberately created as compatibility fixtures.
        evaluated_roles = roles.fallback_roles
        if not evaluated_roles:
            return False
    if overrides is None:
        overrides = _load_tenant_overrides()
    for role in evaluated_roles:
        granted, revoked = _role_grant_revoke(role, overrides)
        if _code_allowed(granted, revoked, code):
            return True
    return False


def get_role_memberships(request: AnyRequest) -> list[Any]:
    """Authorization-active memberships, fetched once and memoized.

    A legacy null ``account_type`` remains active as a compatibility fallback;
    a linked inactive type revokes both its permissions and its scope immediately.
    """
    cached = getattr(request, "_role_memberships_cache", None)
    if cached is not None:
        return cached
    user = getattr(request, "user", None)
    memberships: list[Any]
    if not user or not user.is_authenticated or not bool(getattr(user, "is_active", False)):
        memberships = []
    else:
        memberships_qs = user.role_memberships.filter(revoked_at__isnull=True).filter(
            Q(account_type__isnull=True) | Q(account_type__is_active=True)
        )
        principal_filter = _principal_membership_filter(request, user)
        if principal_filter is False:
            memberships = []
        else:
            if principal_filter is not None:
                memberships_qs = memberships_qs.filter(principal_filter)
            memberships = list(memberships_qs.select_related("account_type", "branch", "department"))
    request._role_memberships_cache = memberships  # type: ignore[union-attr]
    return memberships


_PRINCIPAL_MODELS = {
    "student": "students.StudentProfile",
    "teacher": "teachers.TeacherProfile",
    "parent": "parents.ParentProfile",
    "staff": "org.StaffProfile",
}
_LEGACY_PRINCIPAL_ROLES = {
    "student": frozenset({Role.STUDENT}),
    "teacher": frozenset({Role.TEACHER}),
    "parent": frozenset({Role.PARENT}),
    "staff": frozenset(set(Role.ALL) - {Role.STUDENT, Role.TEACHER, Role.PARENT}),
}


def _principal_membership_filter(request: AnyRequest, user: Any) -> Q | None | bool:
    """Restrict role-native sessions to assignments for that account kind.

    ``User`` is an internal bridge and may legitimately back more than one role
    profile. A student session must therefore never borrow a staff/teacher grant
    merely because those rows share ``user_id``. Missing, malformed, or forged
    metadata fails closed. The legacy union is available only through an
    explicitly marked test adapter and can never be enabled in production.
    """

    has_kind = hasattr(request, "principal_kind")
    has_id = hasattr(request, "principal_id")
    if getattr(request, "_allow_legacy_principal_union_for_tests", False):
        from django.conf import settings

        return None if getattr(settings, "ALLOW_LEGACY_PRINCIPAL_UNION_FOR_TESTS", False) else False
    if not has_kind and not has_id:
        return False
    kind = getattr(request, "principal_kind", "")
    principal_id = getattr(request, "principal_id", None)
    if not kind and principal_id is None:
        return False
    if (
        kind not in _PRINCIPAL_MODELS
        or not isinstance(principal_id, int)
        or isinstance(principal_id, bool)
        or principal_id <= 0
    ):
        return False

    if not getattr(request, "principal_validated", False):
        from django.apps import apps as django_apps

        model = django_apps.get_model(_PRINCIPAL_MODELS[kind])
        if not model.objects.filter(pk=principal_id, user_id=user.pk, is_active=True).exists():
            return False

    return Q(account_type__account_kind=kind) | Q(
        account_type__isnull=True,
        role__in=_LEGACY_PRINCIPAL_ROLES[kind],
    )


def get_user_roles(request: AnyRequest) -> PermissionRoleSet:
    cached = getattr(request, "_user_roles_cache", None)
    if cached is not None:
        return cached
    memberships = get_role_memberships(request)
    account_type_ids = {m.account_type_id for m in memberships if m.account_type_id is not None}
    grants: set[str] = set()
    grants_by_type: dict[int, set[str]] = {account_type_id: set() for account_type_id in account_type_ids}
    if account_type_ids:
        from apps.access.models import AccountTypePermission

        for account_type_id, permission in AccountTypePermission.objects.filter(
            account_type_id__in=account_type_ids
        ).values_list("account_type_id", "permission"):
            grants.add(permission)
            grants_by_type[account_type_id].add(permission)
    legacy_kind_by_role = {
        Role.TEACHER: "teacher",
        Role.STUDENT: "student",
        Role.PARENT: "parent",
    }

    def canonical_role(membership: Any) -> str:
        """Derive compatibility identity from the canonical AccountType.

        ``RoleMembership.role`` is retained for migration compatibility and can
        drift after raw imports.  It must never turn a custom staff type into a
        director/organization-wide authority.  Null AccountTypes are the only
        path that still trusts the legacy role column.
        """
        if membership.account_type_id is None:
            return membership.role
        return membership.account_type.compatibility_role

    membership_scopes = []
    for membership in memberships:
        role = canonical_role(membership)
        membership_scopes.append(
            MembershipGrantScope(
                branch_id=membership.branch_id,
                department_id=membership.department_id,
                role=role,
                account_kind=(
                    membership.account_type.account_kind
                    if membership.account_type_id is not None
                    else legacy_kind_by_role.get(role, "staff")
                ),
                grants=frozenset(grants_by_type.get(membership.account_type_id, set())),
                is_legacy_fallback=membership.account_type_id is None,
                is_organization_wide=(
                    membership.account_type.is_owner_type
                    if membership.account_type_id is not None
                    else role == Role.DIRECTOR
                ),
            )
        )
    roles = PermissionRoleSet(
        (membership.role for membership in membership_scopes),
        canonical_grants=grants,
        fallback_roles=(m.role for m in memberships if m.account_type_id is None),
        account_kinds=(scope.account_kind for scope in membership_scopes),
        membership_scopes=membership_scopes,
    )
    request._user_roles_cache = roles  # type: ignore[union-attr]
    return roles


def get_user_authorization_context(
    user: Any,
    *,
    principal_kind: str,
    principal_id: int,
    principal_validated: bool = False,
) -> tuple[PermissionRoleSet, list[Any]]:
    """Build exact live authorization for one role-native principal.

    Background work must carry the principal snapshot captured by the creating
    request. Reconstructing grants from ``user_id`` alone is unsafe because the
    bridge may back multiple role accounts. Invalid snapshots return an empty
    context rather than borrowing another role's assignments.
    """

    context = SimpleNamespace(
        user=user,
        principal_kind=principal_kind,
        principal_id=principal_id,
        principal_validated=principal_validated,
    )
    roles = get_user_roles(context)  # type: ignore[arg-type]
    return roles, get_role_memberships(context)  # type: ignore[arg-type]


def get_user_roles_for_user(
    user: Any,
    *,
    principal_kind: str,
    principal_id: int,
    principal_validated: bool = False,
) -> PermissionRoleSet:
    """Return canonical grants for an explicit non-request role principal."""

    roles, _memberships = get_user_authorization_context(
        user,
        principal_kind=principal_kind,
        principal_id=principal_id,
        principal_validated=principal_validated,
    )
    return roles


def get_unambiguous_user_authorization_context(user: Any) -> tuple[PermissionRoleSet, list[Any]]:
    """Conservatively resolve a background actor with no stored session snapshot.

    This is a compatibility boundary for older domain jobs. It succeeds only
    when exactly one active role-native profile belongs to the bridge; zero or
    multiple profiles return an empty context. New jobs should persist and call
    :func:`get_user_authorization_context` with explicit kind/id instead.
    """

    from django.apps import apps as django_apps

    active: list[tuple[str, int]] = []
    for kind, label in _PRINCIPAL_MODELS.items():
        model = django_apps.get_model(label)
        profile_id = (
            model.objects.filter(
                user_id=getattr(user, "pk", None),
                user__is_active=True,
                is_active=True,
            )
            .values_list("pk", flat=True)
            .first()
        )
        if profile_id is not None:
            active.append((kind, profile_id))
            if len(active) > 1:
                return PermissionRoleSet(), []
    if len(active) != 1:
        return PermissionRoleSet(), []
    kind, principal_id = active[0]
    return get_user_authorization_context(
        user,
        principal_kind=kind,
        principal_id=principal_id,
        principal_validated=True,
    )


def get_unambiguous_user_roles(user: Any) -> PermissionRoleSet:
    """Role-only companion for legacy background paths; zero/many profiles deny."""

    roles, _memberships = get_unambiguous_user_authorization_context(user)
    return roles


def get_session_authorization_context(session: Any) -> tuple[PermissionRoleSet, list[Any]]:
    """Build exact authorization from a previously validated Session snapshot."""

    kind = str(getattr(session, "principal_kind", "") or "")
    principal_id = getattr(session, "principal_id", None)
    if (
        kind not in _PRINCIPAL_MODELS
        or not isinstance(principal_id, int)
        or isinstance(principal_id, bool)
        or principal_id <= 0
    ):
        return PermissionRoleSet(), []
    return get_user_authorization_context(
        session.user,
        principal_kind=kind,
        principal_id=principal_id,
        principal_validated=False,
    )


def get_legacy_union_roles_for_tests(user: Any) -> PermissionRoleSet:
    """Explicit test-only adapter for pre-role-profile migration fixtures."""

    from django.conf import settings

    if not getattr(settings, "ALLOW_LEGACY_PRINCIPAL_UNION_FOR_TESTS", False):
        raise RuntimeError("Legacy principal unions are disabled outside the test settings.")
    context = SimpleNamespace(user=user, _allow_legacy_principal_union_for_tests=True)
    return get_user_roles(context)  # type: ignore[arg-type]


def get_effective_permission_context(
    request: AnyRequest,
) -> tuple[tuple[str, ...], tuple[EffectivePermissionScope, ...]]:
    """Return the caller's effective permission union and truthful row scopes.

    This is a read model over the same active memberships, canonical account-type
    grants, and live compatibility overrides used by request authorization.  It
    is suitable for UI hints only; endpoint authorization remains authoritative.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return (), ()
    if user.is_superuser:
        return ("*:*",), (
            EffectivePermissionScope(
                branch_id=None,
                department_id=None,
                permissions=("*:*",),
            ),
        )

    roles = get_user_roles(request)
    overrides = _request_overrides(request) if roles.fallback_roles else {}
    aggregate: set[str] = set()
    organization_wide: set[str] = set()
    by_boundary: dict[tuple[int, int | None], set[str]] = {}

    for membership in roles.membership_scopes:
        if membership.is_legacy_fallback:
            granted, revoked = _role_grant_revoke(membership.role, overrides)
        else:
            granted, revoked = set(membership.grants), set()
        permissions = _flat_effective_grants(granted, revoked)
        aggregate.update(permissions)
        if membership.is_organization_wide:
            organization_wide.update(permissions)
        else:
            by_boundary.setdefault((membership.branch_id, membership.department_id), set()).update(
                permissions
            )

    scope_rows: list[EffectivePermissionScope] = []
    if organization_wide:
        scope_rows.append(
            EffectivePermissionScope(
                branch_id=None,
                department_id=None,
                permissions=tuple(sorted(organization_wide)),
            )
        )

    scope_rows.extend(
        EffectivePermissionScope(
            branch_id=branch_id,
            department_id=department_id,
            permissions=tuple(sorted(permissions - organization_wide)),
        )
        for (branch_id, department_id), permissions in sorted(
            by_boundary.items(),
            key=lambda item: (item[0][0], item[0][1] is not None, item[0][1] or 0),
        )
        if permissions - organization_wide
    )
    return tuple(sorted(aggregate)), tuple(scope_rows)


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
        roles = get_user_roles(request)
        overrides = _request_overrides(request) if roles.fallback_roles else {}
        return has_permission_code(roles, required, overrides)


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def is_read_only_token(request: Request) -> bool:
    """Return whether the request uses a read-only impersonation session.

    The session authenticator exposes ``request.is_read_only_token``. The raw auth
    mapping fallback keeps this helper compatible with focused permission tests.
    """
    read_only = bool(getattr(request, "is_read_only_token", False))
    if not read_only:
        auth = getattr(request, "auth", None)
        try:
            read_only = bool(auth.get("read_only")) if auth is not None else False
        except AttributeError:
            read_only = False
    return read_only


class DenyWriteForReadOnlyToken(BasePermission):
    """Allow only safe methods under a read-only impersonation session.

    Any write returns 403 ``read_only_token``. Ordinary sessions are unaffected.

    Layered views enforce this centrally in ``SessionAuthentication``; this class
    remains for the reports DRF viewsets."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        if request.method in SAFE_METHODS:
            return True
        if is_read_only_token(request):
            from core.exceptions import PermissionException

            raise PermissionException(code="read_only_token")
        return True
