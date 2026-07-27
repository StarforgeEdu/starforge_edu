"""Content read selectors: visibility-scoped library/file queries + the storage
quota meter."""

from __future__ import annotations

from django.db.models import Q, QuerySet, Sum

from apps.content.models import ContentLibrary, LessonFile
from core.permissions import Role

STAFF_ROLES = {Role.DIRECTOR}

# F4-5 content review/publication. REVIEWER_ROLES are the content staff (vs
# learners): they may see files still pending dual approval and may download a
# view-only file. Reach differs by role: a DIRECTOR sees every file tenant-wide;
# a HEAD_OF_DEPT (manager) stays department-scoped like everywhere else but
# additionally reaches any file still pending the manager sign-off so they can
# counter-sign content anywhere — they get NO blanket read of already-published
# content in libraries outside their scope. A TEACHER/LIBRARIAN sees drafts only
# within their own library visibility. Everyone else (students, parents,
# non-academic staff) sees only dual-approved, published files.
REVIEWER_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT, Role.TEACHER, Role.LIBRARIAN}


def _related_cohort_ids(user) -> set[int]:
    """Cohorts the user belongs to: student member, parent of a member, or a
    teacher of the cohort."""
    from apps.cohorts.models import Cohort

    qs = Cohort.objects.filter(
        Q(memberships__student__user=user, memberships__end_date__isnull=True)
        | Q(
            memberships__student__guardians__parent__user=user,
            memberships__end_date__isnull=True,
        )
        | Q(primary_teacher__user=user)
        | Q(co_teachers__teacher__user=user)
        | Q(lessons__teacher__user=user)
    )
    return set(qs.values_list("id", flat=True))


def _visibility_filter(user, roles: set[str], memberships) -> Q:
    """Q over ContentLibrary matching what `user` may see (TD-13 visibility)."""
    dept_ids = {m.department_id for m in memberships if m.department_id}
    cohort_ids = _related_cohort_ids(user)
    q = Q(visibility=ContentLibrary.Visibility.TENANT)
    q |= Q(visibility=ContentLibrary.Visibility.DEPARTMENT, department_id__in=dept_ids)
    q |= Q(visibility=ContentLibrary.Visibility.COHORT, cohort_id__in=cohort_ids)
    for role in roles:  # role visibility: allowed_roles JSON contains the role
        q |= Q(visibility=ContentLibrary.Visibility.ROLE, allowed_roles__contains=role)
    return q


def scoped_libraries(*, user, roles: set[str] | None = None, memberships=None) -> QuerySet[ContentLibrary]:
    # select_related the labelled FKs the presenter dereferences (department/cohort
    # names) — no N+1 on the list, and harmless when this qs is used as an `__in=`
    # subquery elsewhere (Django selects only the pk there).
    qs = ContentLibrary.objects.filter(is_active=True).select_related("department", "cohort")
    if user.is_superuser:
        return qs
    if memberships is None:
        memberships = list(user.role_memberships.filter(revoked_at__isnull=True))
    if roles is None:
        roles = {m.role for m in memberships}
    if roles & STAFF_ROLES:
        return qs
    return qs.filter(_visibility_filter(user, roles, memberships)).distinct()


def scoped_files(*, user, roles: set[str] | None = None, memberships=None) -> QuerySet[LessonFile]:
    qs = LessonFile.objects.select_related("lesson", "folder", "uploaded_by")
    if user.is_superuser:
        return qs
    if memberships is None:
        memberships = list(user.role_memberships.filter(revoked_at__isnull=True))
    if roles is None:
        roles = {m.role for m in memberships}
    if Role.DIRECTOR in roles:
        return qs  # director: every file tenant-wide
    libs = scoped_libraries(user=user, roles=roles, memberships=memberships)
    visible = Q(lesson__module__course__library__in=libs) | Q(folder__library__in=libs)
    if Role.HEAD_OF_DEPT in roles:
        # Manager: own visibility scope PLUS any file still pending the manager
        # sign-off (so they can counter-sign content anywhere) — least privilege,
        # no blanket read of published content outside their scope (F4-5 review).
        return qs.filter(visible | Q(is_approved_manager=False)).distinct()
    if roles & REVIEWER_ROLES:
        # Teacher/librarian: drafts within their library scope (they review/sign).
        return qs.filter(visible).distinct()
    # Learners (and any non-reviewer): only dual-approved, published files.
    return qs.filter(visible, is_approved_teacher=True, is_approved_manager=True).distinct()


def storage_used_bytes() -> int:
    """Total bytes of CLEAN files in the current tenant (D3-E billing meter)."""
    return (
        LessonFile.objects.filter(status=LessonFile.Status.CLEAN).aggregate(total=Sum("size_bytes"))["total"]
        or 0
    )
