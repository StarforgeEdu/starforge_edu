"""Content read selectors: visibility-scoped library/file queries + the storage
quota meter."""

from __future__ import annotations

from django.db.models import Q, QuerySet, Sum

from apps.content.models import ContentLibrary, LessonFile
from core.permissions import (
    PermissionRoleSet,
    Role,
    get_unambiguous_user_authorization_context,
    has_permission_code,
)
from core.scoping import permission_membership_is_unscoped, permission_membership_scope_q

# F4-5 content review/publication. REVIEWER_ROLES are the content staff (vs
# learners): they may see files still pending dual approval and may download a
# view-only file. Reach differs by role: a DIRECTOR sees every file tenant-wide;
# a HEAD_OF_DEPT (manager) stays department-scoped like everywhere else and
# never reaches pending files outside the membership granting approval. A
# TEACHER/LIBRARIAN sees drafts only within their own library visibility.
# Everyone else sees only dual-approved, published files.
REVIEWER_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT, Role.TEACHER, Role.LIBRARIAN}
_MANAGER_APPROVAL_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT}
_CONTENT_MANAGEMENT_PERMISSIONS = ("content:write", "content:approve", "content:publish")


def has_global_content_scope(roles: set[str], *, permission: str = "content:read") -> bool:
    """Whether one organization-wide membership supplies ``permission``."""
    if isinstance(roles, PermissionRoleSet):
        return permission_membership_is_unscoped(
            roles=roles,
            permission=permission,
            account_kinds={"staff", "teacher"},
        )
    return Role.DIRECTOR in roles


def can_review_content(roles: set[str]) -> bool:
    """Whether drafts should be visible for an approval workflow."""
    if isinstance(roles, PermissionRoleSet):
        return has_permission_code(roles, "content:approve") or has_permission_code(roles, "content:publish")
    return bool(roles & REVIEWER_ROLES)


def can_publish_content(roles: set[str]) -> bool:
    """Whether the caller may provide the elevated second sign-off.

    The manager leg requires an explicit ``content:publish`` grant (or the owner
    wildcard). A broad ``content:*`` authoring grant still permits the first
    review, but does not silently turn a teacher or librarian into the second
    maker-checker signer.
    """
    if not isinstance(roles, PermissionRoleSet):
        return bool(roles & _MANAGER_APPROVAL_ROLES)
    for membership in roles.membership_scopes:
        if membership.is_legacy_fallback:
            if membership.role in _MANAGER_APPROVAL_ROLES:
                return True
            continue
        if "*:*" in membership.grants or "content:publish" in membership.grants:
            return True
    return False


def _related_cohort_ids(user) -> set[int]:
    """Cohorts the user belongs to: student member, parent of a member, or a
    teacher of the cohort."""
    from apps.cohorts.models import Cohort
    from apps.cohorts.selectors import taught_cohorts

    qs = Cohort.objects.filter(
        Q(memberships__student__user=user, memberships__end_date__isnull=True)
        | Q(
            memberships__student__guardians__parent__user=user,
            memberships__student__guardians__revoked_at__isnull=True,
            memberships__end_date__isnull=True,
        )
        | Q(pk__in=taught_cohorts(user=user).values("pk"))
    )
    return set(qs.values_list("id", flat=True))


def _visibility_filter(user, roles: set[str], memberships, *, permission: str) -> Q:
    """Build the library scope for one exact operation.

    Tenant- and role-visible libraries are organization-wide mutation targets.
    Scoped users may read them when they are in the intended audience, but they
    cannot modify them by borrowing a branch-level grant.
    """
    read = permission == "content:read"
    dept_ids = {m.department_id for m in memberships if m.department_id}
    cohort_ids = _related_cohort_ids(user) if read else set()
    q = Q(visibility=ContentLibrary.Visibility.TENANT) if read else Q(pk__in=[])
    if isinstance(roles, PermissionRoleSet):
        department_scope = permission_membership_scope_q(
            roles=roles,
            permission=permission,
            branch_field="department__branch_id",
            department_field="department_id",
            account_kinds={"staff", "teacher"},
        )
        cohort_scope = permission_membership_scope_q(
            roles=roles,
            permission=permission,
            branch_field="cohort__branch_id",
            department_field="cohort__department_id",
            account_kinds={"staff", "teacher"},
        )
        q |= Q(visibility=ContentLibrary.Visibility.DEPARTMENT) & department_scope
        q |= Q(visibility=ContentLibrary.Visibility.COHORT) & cohort_scope
    else:
        # Direct selector calls/tests may pass a plain role set (legacy contract).
        q |= Q(visibility=ContentLibrary.Visibility.DEPARTMENT, department_id__in=dept_ids)
    if read:
        q |= Q(visibility=ContentLibrary.Visibility.COHORT, cohort_id__in=cohort_ids)
    # A canonical custom STAFF type must not inherit its compatibility SUPPORT
    # scope. Teacher/student/parent are natural identity relationships and retain
    # their legacy role-library visibility during this transition.
    visible_roles = roles
    if isinstance(roles, PermissionRoleSet):
        natural_role_by_kind = {
            "teacher": Role.TEACHER,
            "student": Role.STUDENT,
            "parent": Role.PARENT,
        }
        visible_roles = {
            natural_role_by_kind[kind] for kind in roles.account_kinds if kind in natural_role_by_kind
        }
    if read:
        for role in visible_roles:  # role visibility: allowed_roles JSON contains the role
            q |= Q(visibility=ContentLibrary.Visibility.ROLE, allowed_roles__contains=role)
    return q


def scoped_libraries(
    *,
    user,
    roles: set[str] | None = None,
    memberships=None,
    permission: str = "content:read",
) -> QuerySet[ContentLibrary]:
    # select_related the labelled FKs the presenter dereferences (department/cohort
    # names) — no N+1 on the list, and harmless when this qs is used as an `__in=`
    # subquery elsewhere (Django selects only the pk there).
    # Managers must retain a path to an inactive library so it can be audited or
    # reactivated through the API.  Ordinary readers still see active libraries
    # only; applying the active filter before the manager bypass made deactivation
    # an irreversible API operation.
    qs = ContentLibrary.objects.select_related("department", "cohort")
    if user.is_superuser:
        return qs
    if roles is None:
        roles, canonical_memberships = get_unambiguous_user_authorization_context(user)
        if memberships is None:
            memberships = canonical_memberships
    elif memberships is None:
        memberships = list(user.role_memberships.filter(revoked_at__isnull=True))
    if has_global_content_scope(roles, permission=permission):
        return qs
    if permission == "content:read":
        qs = qs.filter(is_active=True)
    return qs.filter(_visibility_filter(user, roles, memberships, permission=permission)).distinct()


def scoped_files(
    *,
    user,
    roles: set[str] | None = None,
    memberships=None,
    permission: str = "content:read",
) -> QuerySet[LessonFile]:
    qs = LessonFile.objects.select_related("lesson", "folder", "uploaded_by")
    if user.is_superuser:
        return qs
    if roles is None:
        roles, canonical_memberships = get_unambiguous_user_authorization_context(user)
        if memberships is None:
            memberships = canonical_memberships
    elif memberships is None:
        memberships = list(user.role_memberships.filter(revoked_at__isnull=True))
    if has_global_content_scope(roles, permission=permission):
        return qs  # protected owner: every file tenant-wide

    # A teacher may explicitly submit a draft to a tenant-wide library for a
    # distinct organization-wide publisher to countersign. Tenant libraries are
    # intentionally not ordinary branch-scoped mutation targets, so retain one
    # narrow exception for files uploaded by this exact teacher account. The
    # PermissionRoleSet is principal-scoped by the authenticated session: a staff
    # session sharing the same bridge User cannot inherit this path.
    teacher_owned_global = Q(pk__in=[])
    if (
        isinstance(roles, PermissionRoleSet)
        and "teacher" in roles.account_kinds
        and has_permission_code(roles, permission)
    ):
        teacher_owned_global = Q(
            uploaded_by=user,
            submitted_by_teacher__user=user,
            submission_audience=LessonFile.SubmissionAudience.GLOBAL,
        ) & (
            Q(folder__library__visibility=ContentLibrary.Visibility.TENANT)
            | Q(lesson__module__course__library__visibility=ContentLibrary.Visibility.TENANT)
        )
    libs = scoped_libraries(
        user=user,
        roles=roles,
        memberships=memberships,
        permission=permission,
    )
    visible = Q(lesson__module__course__library__in=libs) | Q(folder__library__in=libs)
    if permission != "content:read":
        return qs.filter(visible | teacher_owned_global).distinct()
    if isinstance(roles, PermissionRoleSet):
        managed = Q(pk__in=[])
        for management_permission in _CONTENT_MANAGEMENT_PERMISSIONS:
            managed_libraries = scoped_libraries(
                user=user,
                roles=roles,
                memberships=memberships,
                permission=management_permission,
            )
            managed |= Q(lesson__module__course__library__in=managed_libraries) | Q(
                folder__library__in=managed_libraries
            )
        published = Q(is_approved_teacher=True, is_approved_manager=True)
        return qs.filter(visible & (published | managed | teacher_owned_global)).distinct()
    if can_review_content(roles):
        # Canonical reviewer/publisher grants and legacy teacher/librarian roles
        # see drafts only within their exact library scope.
        return qs.filter(visible).distinct()
    # Learners (and any non-reviewer): only dual-approved, published files.
    return qs.filter(visible, is_approved_teacher=True, is_approved_manager=True).distinct()


def managed_files(*, user, roles: set[str] | None = None) -> QuerySet[LessonFile]:
    """Files covered by at least one exact content-management grant."""

    scope = Q(pk__in=[])
    for permission in _CONTENT_MANAGEMENT_PERMISSIONS:
        scope |= Q(pk__in=scoped_files(user=user, roles=roles, permission=permission).values("pk"))
    return LessonFile.objects.filter(scope).distinct()


def storage_used_bytes() -> int:
    """Total bytes of CLEAN files in the current tenant (D3-E billing meter)."""
    return (
        LessonFile.objects.filter(status=LessonFile.Status.CLEAN).aggregate(total=Sum("size_bytes"))["total"]
        or 0
    )
