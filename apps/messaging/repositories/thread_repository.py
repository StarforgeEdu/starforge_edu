"""ORM-backed messaging repository — participant-scoped thread reads."""

from __future__ import annotations

from django.db.models import Count, Exists, F, Min, OuterRef, Prefetch, Q, QuerySet, Subquery

from apps.access.models import AccountType
from apps.cohorts.selectors import taught_cohorts
from apps.messaging.dto.thread_dto import ThreadEventPageDTO
from apps.messaging.interfaces.repositories import IThreadRepository
from apps.messaging.models import (
    DELIVERABLE_PARTICIPANT_STATUSES,
    Message,
    MessageReaction,
    Thread,
    ThreadParticipant,
    ThreadRealtimeEvent,
)
from apps.org.models import StaffProfile
from apps.parents.models import ParentProfile
from apps.students.models import StudentProfile
from apps.teachers.models import TeacherProfile
from apps.users.models import RoleMembership, User
from core.permissions import Role, get_user_roles
from core.repositories import BaseRepository
from core.scoping import permission_membership_scopes

_NON_STAFF_ROLES = {Role.STUDENT, Role.PARENT}
_MANAGEMENT_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT}
_LEGACY_STAFF_TEACHER_ROLES = tuple(role for role in Role.ALL if role not in _NON_STAFF_ROLES)
_MANAGEMENT_ACCOUNT_TYPE_SLUG = (
    Q(account_type__slug__icontains="ceo")
    | Q(account_type__slug__icontains="owner")
    | Q(account_type__slug__icontains="director")
    | Q(account_type__slug__icontains="manager")
    | Q(account_type__slug__icontains="head_of_dept")
    | Q(account_type__slug__icontains="head-of-dept")
    | Q(account_type__slug__iregex=r"(^|[-_])hod($|[-_])")
)


class ThreadRepository(BaseRepository[Thread], IThreadRepository):
    model = Thread

    def participant_threads(self, *, user, principal_kind: str, principal_id: int) -> QuerySet[Thread]:
        # Strict isolation: only threads the exact role account joined are resolvable,
        # so every detail/action is participant-gated by construction. `messages` is NOT
        # prefetched: it is append-only/unbounded and was only used to count unread — a
        # page of long threads would load tens of thousands of message rows just to produce
        # a few integers. Unread is now one bounded query (unread_counts). `participants` is
        # small and stays prefetched (the presenter emits the roster).
        return (
            Thread.objects.filter(
                participants__user_id=user.pk,
                participants__principal_kind=principal_kind,
                participants__principal_id=principal_id,
                participants__attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
                participants__hidden_at__isnull=True,
            )
            .distinct()
            .select_related("branch", "created_by")
            .prefetch_related(
                Prefetch(
                    "participants",
                    queryset=ThreadParticipant.objects.filter(
                        attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES
                    ),
                )
            )
        )

    def unread_counts(
        self,
        *,
        thread_ids: list[int],
        viewer_id: int,
        viewer_principal_kind: str,
        viewer_principal_id: int,
    ) -> dict[int, int]:
        """{thread_id: unread_count} for `viewer_id` across the given threads in ONE query.

        Unread = messages from OTHERS newer than the viewer's own last_read for that thread
        (a null last_read means everything from others is unread) — the exact semantics the
        old per-row Python count had, but bounded to the page's threads and served by the
        Message(thread, created_at) index instead of loading every message row."""
        if not thread_ids:
            return {}
        viewer_participant = ThreadParticipant.objects.filter(
            thread_id=OuterRef("thread_id"),
            user_id=viewer_id,
            principal_kind=viewer_principal_kind,
            principal_id=viewer_principal_id,
            attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
        )
        viewer_last_read_message = viewer_participant.values("last_read_message_id")[:1]
        viewer_last_read_at = viewer_participant.values("last_read_at")[:1]
        rows = (
            Message.objects.filter(thread_id__in=thread_ids)
            .exclude(sender_id=viewer_id)
            .annotate(
                _viewer_last_read_message=Subquery(viewer_last_read_message),
                _viewer_last_read_at=Subquery(viewer_last_read_at),
            )
            .filter(
                Q(
                    _viewer_last_read_message__isnull=False,
                    id__gt=F("_viewer_last_read_message"),
                )
                | Q(
                    _viewer_last_read_message__isnull=True,
                    _viewer_last_read_at__isnull=True,
                )
                | Q(
                    _viewer_last_read_message__isnull=True,
                    _viewer_last_read_at__isnull=False,
                    created_at__gt=F("_viewer_last_read_at"),
                )
            )
            .values("thread_id")
            .annotate(n=Count("id"))
        )
        return {row["thread_id"]: row["n"] for row in rows}

    def get_participant_thread(
        self, *, user, principal_kind: str, principal_id: int, pk: int
    ) -> Thread | None:
        return (
            self.participant_threads(
                user=user,
                principal_kind=principal_kind,
                principal_id=principal_id,
            )
            .filter(pk=pk)
            .first()
        )

    def messages_of(self, *, thread: Thread) -> QuerySet[Message]:
        return (
            Message.objects.filter(thread=thread)
            .select_related("sender")
            .prefetch_related(
                Prefetch(
                    "reactions",
                    queryset=MessageReaction.objects.filter(removed_at__isnull=True),
                    to_attr="active_reactions",
                )
            )
        )

    def get_participant_message(
        self,
        *,
        user,
        principal_kind: str,
        principal_id: int,
        pk: int,
    ) -> Message | None:
        return (
            Message.objects.filter(
                pk=pk,
                thread__participants__user_id=user.pk,
                thread__participants__principal_kind=principal_kind,
                thread__participants__principal_id=principal_id,
                thread__participants__attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
            )
            .select_related("sender", "thread")
            .prefetch_related(
                Prefetch(
                    "reactions",
                    queryset=MessageReaction.objects.filter(removed_at__isnull=True),
                    to_attr="active_reactions",
                )
            )
            .first()
        )

    def event_page(
        self,
        *,
        thread: Thread,
        after: int,
        limit: int,
    ) -> ThreadEventPageDTO:
        """Return one stable page bounded by a single committed high watermark."""

        high_watermark = int(
            Thread.objects.filter(pk=thread.pk).values_list("realtime_sequence", flat=True).get()
        )
        base = ThreadRealtimeEvent.objects.filter(thread_id=thread.pk)
        first_sequence = base.aggregate(value=Min("sequence"))["value"]
        recovery_floor = int(first_sequence) if first_sequence is not None else high_watermark + 1
        reset_required = high_watermark > 0 and after < recovery_floor - 1
        events: tuple[ThreadRealtimeEvent, ...] = ()
        has_more = False
        next_cursor = after
        if not reset_required:
            rows = list(
                base.filter(sequence__gt=after, sequence__lte=high_watermark).order_by("sequence")[
                    : limit + 1
                ]
            )
            has_more = len(rows) > limit
            events = tuple(rows[:limit])
            if events:
                next_cursor = events[-1].sequence
        return ThreadEventPageDTO(
            events=events,
            requested_after=after,
            next_cursor=next_cursor,
            high_watermark=high_watermark,
            recovery_floor=recovery_floor,
            has_more=has_more,
            reset_required=reset_required,
        )

    def is_participant(
        self,
        *,
        thread_id: int,
        user_id: int,
        principal_kind: str,
        principal_id: int,
    ) -> bool:
        return ThreadParticipant.objects.filter(
            thread_id=thread_id,
            user_id=user_id,
            principal_kind=principal_kind,
            principal_id=principal_id,
            attribution_status__in=DELIVERABLE_PARTICIPANT_STATUSES,
        ).exists()

    def active_members(self, *, ids: list[int]) -> list[User]:
        # Participants must be active members of THIS center — never a membership-less /
        # cross-tenant user row. Exists() (not a role_memberships__isnull filter, which a
        # LEFT JOIN would let membership-less users slip through).
        active_member = RoleMembership.objects.filter(user_id=OuterRef("pk"), revoked_at__isnull=True)
        return list(User.objects.filter(id__in=ids, is_active=True).filter(Exists(active_member)))

    def _recipient_membership_scope(self, *, authorization_context):
        roles = get_user_roles(authorization_context)
        write_scopes = permission_membership_scopes(
            roles=roles,
            permission="messaging:write",
        )
        recipient_membership_scope = Q(pk__in=[])
        if any(scope.is_organization_wide for scope in write_scopes):
            recipient_membership_scope = Q()
        else:
            for scope in write_scopes:
                membership_scope = Q(branch_id=scope.branch_id)
                if scope.department_id is not None:
                    membership_scope &= Q(department_id=scope.department_id)
                recipient_membership_scope |= membership_scope
        return write_scopes, recipient_membership_scope

    def recipient_scope_principals(
        self, *, authorization_context, principals: list
    ) -> set[tuple[int, str, int]]:
        """Active role principals inside the exact messaging-write grant.

        This intentionally checks the recipient's membership at the sender's
        permission-bearing branch/department boundary.  A second, unrelated
        membership must never be able to lend its scope to the write grant.
        """
        if not principals:
            return set()
        ids = {principal.user_id for principal in principals}
        _write_scopes, membership_scope = self._recipient_membership_scope(
            authorization_context=authorization_context
        )
        memberships = (
            RoleMembership.objects.filter(
                user_id__in=ids,
                user__is_active=True,
                revoked_at__isnull=True,
            )
            .filter(membership_scope)
            .filter(Q(account_type__isnull=True) | Q(account_type__is_active=True))
        )
        legacy_kind_by_role = {
            Role.TEACHER: AccountType.AccountKind.TEACHER,
            Role.STUDENT: AccountType.AccountKind.STUDENT,
            Role.PARENT: AccountType.AccountKind.PARENT,
        }
        eligible: set[tuple[int, str]] = set()
        for membership in memberships.select_related("account_type"):
            account_type = membership.account_type
            kind = (
                account_type.account_kind
                if account_type is not None
                else legacy_kind_by_role.get(membership.role, AccountType.AccountKind.STAFF)
            )
            eligible.add((membership.user_id, str(kind)))
        return {
            (principal.user_id, principal.kind, principal.principal_id)
            for principal in principals
            if (principal.user_id, principal.kind) in eligible
        }

    def is_active_teacher(self, *, authorization_context) -> bool:
        """Whether the signed-in principal is an active role-native teacher.

        Looking only for a TeacherProfile on the bridge User lets a student or
        staff session borrow that teacher profile's student directory.
        """
        from core.role_principals import request_role_principal

        user = authorization_context.user
        principal = request_role_principal(
            authorization_context,
            error_code="messaging_principal_unavailable",
        )
        write_scopes, _membership_scope = self._recipient_membership_scope(
            authorization_context=authorization_context
        )
        return (
            principal.kind == AccountType.AccountKind.TEACHER
            and any(scope.account_kind == AccountType.AccountKind.TEACHER for scope in write_scopes)
            and TeacherProfile.objects.filter(
                user=user,
                pk=principal.principal_id,
                is_active=True,
            ).exists()
        )

    def is_active_staff(self, *, authorization_context) -> bool:
        """Whether the signed-in principal is an active role-native staff account."""
        from core.role_principals import request_role_principal

        user = authorization_context.user
        principal = request_role_principal(
            authorization_context,
            error_code="messaging_principal_unavailable",
        )
        write_scopes, _membership_scope = self._recipient_membership_scope(
            authorization_context=authorization_context
        )
        return (
            principal.kind == AccountType.AccountKind.STAFF
            and any(scope.account_kind == AccountType.AccountKind.STAFF for scope in write_scopes)
            and StaffProfile.objects.filter(
                user=user,
                pk=principal.principal_id,
                is_active=True,
            ).exists()
        )

    def contacts_for(self, *, authorization_context, category: str = "") -> QuerySet[User]:
        """Purpose-limited messaging directory.

        Every returned primary key is a real ``users.User`` bridge id accepted by
        thread creation. Staff/teacher contacts are active role-native accounts.
        An active teacher sees only students in cohorts they actually teach. Active
        staff see students inside the exact branch/department scope that grants
        ``messaging:write``; organization-wide Directors therefore see every active
        student in the tenant.
        """
        user = authorization_context.user
        write_scopes, recipient_membership_scope = self._recipient_membership_scope(
            authorization_context=authorization_context
        )

        active_staff_membership = (
            RoleMembership.objects.filter(user_id=OuterRef("pk"), revoked_at__isnull=True)
            .filter(recipient_membership_scope)
            .filter(
                Q(
                    account_type__is_active=True,
                    account_type__account_kind__in=(
                        AccountType.AccountKind.STAFF,
                        AccountType.AccountKind.TEACHER,
                    ),
                )
                | Q(account_type__isnull=True, role__in=_LEGACY_STAFF_TEACHER_ROLES)
            )
        )
        active_student_membership = (
            RoleMembership.objects.filter(user_id=OuterRef("pk"), revoked_at__isnull=True)
            .filter(recipient_membership_scope)
            .filter(
                Q(
                    account_type__is_active=True,
                    account_type__account_kind=AccountType.AccountKind.STUDENT,
                )
                | Q(account_type__isnull=True, role=Role.STUDENT)
            )
        )
        active_parent_membership = (
            RoleMembership.objects.filter(user_id=OuterRef("pk"), revoked_at__isnull=True)
            .filter(recipient_membership_scope)
            .filter(
                Q(
                    account_type__is_active=True,
                    account_type__account_kind=AccountType.AccountKind.PARENT,
                )
                | Q(account_type__isnull=True, role=Role.PARENT)
            )
        )
        management_membership = RoleMembership.objects.filter(
            user_id=OuterRef("pk"), revoked_at__isnull=True
        ).filter(
            (Q(account_type__is_active=True) & _MANAGEMENT_ACCOUNT_TYPE_SLUG)
            | Q(account_type__isnull=True, role__in=_MANAGEMENT_ROLES)
        )

        qs = (
            User.objects.filter(is_active=True)
            .exclude(pk=user.pk)
            .annotate(
                contact_is_staff=Exists(active_staff_membership),
                contact_is_student=Exists(active_student_membership),
                contact_is_parent=Exists(active_parent_membership),
                contact_is_management=Exists(management_membership),
            )
            .select_related(
                "staff_profile",
                "teacher_profile",
                "student_profile__current_cohort",
                "parent_profile",
            )
        )

        # Negating ``related__is_active=True`` on an absent reverse one-to-one
        # compiles to SQL ``NOT NULL`` in some compound-Q shapes, which is NULL
        # rather than true. Spell out absent-or-inactive so valid single-profile
        # teachers/parents do not disappear from the exact-principal directory.
        no_active_staff = Q(staff_profile__isnull=True) | Q(staff_profile__is_active=False)
        no_active_teacher = Q(teacher_profile__isnull=True) | Q(teacher_profile__is_active=False)
        no_active_student = Q(student_profile__isnull=True) | Q(student_profile__is_active=False)
        no_active_parent = Q(parent_profile__isnull=True) | Q(parent_profile__is_active=False)
        only_staff_profile = (
            Q(staff_profile__is_active=True) & no_active_teacher & no_active_student & no_active_parent
        )
        only_teacher_profile = (
            Q(teacher_profile__is_active=True) & no_active_staff & no_active_student & no_active_parent
        )
        only_student_profile = (
            Q(student_profile__is_active=True) & no_active_staff & no_active_teacher & no_active_parent
        )
        only_parent_profile = (
            Q(parent_profile__is_active=True) & no_active_staff & no_active_teacher & no_active_student
        )
        # Management accounts remain constrained by the sender's exact
        # messaging-write branch/department grant. Teachers need a direct,
        # auditable channel to coordinators and leaders in that same scope.
        staff_visible = Q(contact_is_staff=True) & (only_staff_profile | only_teacher_profile)
        student_visible = Q(pk__in=[])
        parent_visible = Q(pk__in=[])
        active_teacher = self.is_active_teacher(authorization_context=authorization_context)
        active_staff = self.is_active_staff(authorization_context=authorization_context)
        if active_teacher or active_staff:
            student_profile_scope = Q(pk__in=[])
            if any(scope.is_organization_wide for scope in write_scopes):
                student_profile_scope = Q(pk__isnull=False)
            else:
                for scope in write_scopes:
                    profile_scope = Q(current_cohort__branch_id=scope.branch_id)
                    if scope.department_id is not None:
                        profile_scope &= Q(current_cohort__department_id=scope.department_id)
                    student_profile_scope |= profile_scope
            owned_students = StudentProfile.objects.filter(
                student_profile_scope,
                user__is_active=True,
                is_active=True,
                status__in=(StudentProfile.Status.ENROLLED, StudentProfile.Status.ACTIVE),
            )
            if active_teacher:
                owned_students = owned_students.filter(current_cohort__in=taught_cohorts(user=user))
            student_visible = (
                Q(contact_is_student=True, pk__in=owned_students.values("user_id")) & only_student_profile
            )
            related_parent_ids = ParentProfile.objects.filter(
                is_active=True,
                user__is_active=True,
                guardianships__student__in=owned_students,
                guardianships__revoked_at__isnull=True,
            ).values("user_id")
            parent_visible = Q(contact_is_parent=True, pk__in=related_parent_ids) & only_parent_profile

        if category == "staff":
            visible = staff_visible
        elif category == "student":
            visible = student_visible
        elif category == "parent":
            visible = parent_visible
        else:
            visible = staff_visible | student_visible | parent_visible

        active_memberships = (
            RoleMembership.objects.filter(revoked_at__isnull=True)
            .filter(recipient_membership_scope)
            .filter(Q(account_type__isnull=True) | Q(account_type__is_active=True))
            .select_related("account_type")
            .order_by("-granted_at", "-id")
        )
        return (
            qs.filter(visible)
            .prefetch_related(
                Prefetch(
                    "role_memberships",
                    queryset=active_memberships,
                    to_attr="messaging_memberships",
                )
            )
            .order_by("id")
        )
