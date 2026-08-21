"""MeetingService — schedule / cancel / RSVP + role-scoped reads. Reuses the tested
domain fns (schedule_meeting / cancel_meeting / respond_to_meeting) unchanged."""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.meetings.dto.meeting_dto import MeetingInvitee, MeetingPrincipalTarget, ScheduleMeetingDTO
from apps.meetings.interfaces.repositories import IMeetingRepository
from apps.meetings.interfaces.services import IMeetingService
from apps.meetings.models import MeetingAttendee, StaffMeeting
from apps.users.models import User
from core.exceptions import ValidationException


class MeetingService(IMeetingService):
    def __init__(self, meetings: IMeetingRepository) -> None:
        self._meetings = meetings

    def scoped_list(
        self,
        *,
        is_unscoped: bool,
        is_manager: bool,
        branch_ids: set[int],
        principal_kind: str,
        principal_id: int,
    ) -> QuerySet[StaffMeeting]:
        return self._meetings.scoped(
            is_unscoped=is_unscoped,
            is_manager=is_manager,
            branch_ids=branch_ids,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )

    def get_visible(
        self,
        *,
        is_unscoped: bool,
        is_manager: bool,
        branch_ids: set[int],
        principal_kind: str,
        principal_id: int,
        pk: int,
    ) -> StaffMeeting | None:
        return self._meetings.get_scoped(
            is_unscoped=is_unscoped,
            is_manager=is_manager,
            branch_ids=branch_ids,
            principal_kind=principal_kind,
            principal_id=principal_id,
            pk=pk,
        )

    def upcoming_for(self, *, principal_kind: str, principal_id: int) -> QuerySet[StaffMeeting]:
        return self._meetings.upcoming_for(
            principal_kind=principal_kind,
            principal_id=principal_id,
        )

    def schedule(
        self,
        data: ScheduleMeetingDTO,
        *,
        created_by,
        created_by_principal_kind: str,
        created_by_principal_id: int,
        branch,
        attendees: list[MeetingInvitee],
    ) -> StaffMeeting:
        from apps.meetings.services import schedule_meeting

        return schedule_meeting(
            title=data.title,
            agenda=data.agenda,
            starts_at=data.starts_at,
            ends_at=data.ends_at,
            location=data.location,
            attendees=attendees,
            created_by=created_by,
            created_by_principal_kind=created_by_principal_kind,
            created_by_principal_id=created_by_principal_id,
            branch=branch,
        )

    def cancel(
        self,
        meeting: StaffMeeting,
        *,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
    ) -> StaffMeeting:
        from apps.meetings.services import cancel_meeting

        return cancel_meeting(
            meeting_id=meeting.pk,
            actor=actor,
            actor_principal_kind=actor_principal_kind,
            actor_principal_id=actor_principal_id,
        )

    def respond(
        self,
        meeting: StaffMeeting,
        *,
        user,
        principal_kind: str,
        principal_id: int,
        response: str,
    ) -> MeetingAttendee:
        from apps.meetings.services import respond_to_meeting

        return respond_to_meeting(
            meeting_id=meeting.pk,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
            response=response,
        )

    def resolve_branch(self, branch_id: int | None):
        if branch_id is None:
            return None
        from apps.org.models import Branch

        # Archived branches are not assignable (mirrors the old serializer queryset).
        branch = Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
        if branch is None:
            raise ValidationException(
                _("Invalid branch."), code="invalid_branch", fields={"branch": ["Not found."]}
            )
        return branch

    @staticmethod
    def resolve_attendees(
        ids: list[int],
        *,
        principal_targets: list[MeetingPrincipalTarget] | None,
        branch_id: int | None,
    ) -> list[MeetingInvitee]:
        from apps.access.models import AccountType
        from core.permissions import Role, role_memberships_for_account_kinds
        from core.role_principals import (
            STAFF_PRINCIPAL_KINDS,
            resolve_unambiguous_user_principals,
        )

        principal_targets = principal_targets or []
        if bool(ids) == bool(principal_targets):
            raise ValidationException(
                _("Choose either attendees or role-account invitees."),
                code="validation_error",
                fields={"attendees": [_("Provide exactly one invitee selector.")]},
            )
        # Meetings are staff coordination — invitees must be active staff (not students/
        # parents), mirroring the old ScheduleMeetingSerializer attendees queryset.
        deduped = list(dict.fromkeys(ids))
        target_users: dict[tuple[str, int], User] = {}
        if principal_targets:
            from django.apps import apps as django_apps

            from core.role_principals import PRINCIPAL_MODELS

            for kind in sorted(STAFF_PRINCIPAL_KINDS):
                principal_ids = {
                    target.principal_id for target in principal_targets if target.principal_kind == kind
                }
                if not principal_ids:
                    continue
                model = django_apps.get_model(PRINCIPAL_MODELS[kind])
                for profile in model.objects.filter(
                    pk__in=principal_ids,
                    is_active=True,
                    user__is_active=True,
                ).select_related("user"):
                    target_users[(kind, profile.pk)] = profile.user
            if len(target_users) != len(principal_targets):
                raise ValidationException(
                    _("One or more invitees are not active staff role accounts."),
                    code="validation_error",
                    fields={"invitees": [_("Choose active staff role accounts.")]},
                )
            if len({user.pk for user in target_users.values()}) != len(principal_targets):
                raise ValidationException(
                    _("Choose one role account per person."),
                    code="validation_error",
                    fields={"invitees": [_("The same person cannot occupy two meeting seats.")]},
                )
            deduped = [user.pk for user in target_users.values()]
        staff_memberships = role_memberships_for_account_kinds(
            (AccountType.AccountKind.STAFF, AccountType.AccountKind.TEACHER)
        ).filter(user_id__in=deduped)
        if branch_id is not None:
            staff_memberships = staff_memberships.filter(branch_id=branch_id)
        legacy_kinds = {Role.TEACHER: AccountType.AccountKind.TEACHER}
        eligible: dict[tuple[int, str], User] = {}
        for membership in staff_memberships.select_related("user", "account_type"):
            kind = (
                membership.account_type.account_kind
                if membership.account_type_id is not None
                else legacy_kinds.get(membership.role, AccountType.AccountKind.STAFF)
            )
            eligible[(membership.user_id, str(kind))] = membership.user
        invalid = ValidationException(
            _("One or more attendees are not valid staff in the meeting's scope."),
            code="validation_error",
            fields={"attendees": [_("Choose active staff recipients in the meeting's scope.")]},
        )
        if not set(deduped).issubset({user_id for user_id, _kind in eligible}):
            raise invalid
        if principal_targets:
            if any(
                (target_users[(target.principal_kind, target.principal_id)].pk, target.principal_kind)
                not in eligible
                for target in principal_targets
            ):
                raise invalid
            return [
                MeetingInvitee(
                    user=target_users[(target.principal_kind, target.principal_id)],
                    principal_kind=target.principal_kind,
                    principal_id=target.principal_id,
                )
                for target in principal_targets
            ]
        try:
            principals = resolve_unambiguous_user_principals(
                deduped,
                allowed_kinds=STAFF_PRINCIPAL_KINDS,
                field="attendees",
                message=_("Choose active staff recipients in the meeting's scope."),
            )
        except ValidationException:
            raise invalid from None
        if any((principal.user_id, principal.kind) not in eligible for principal in principals.values()):
            raise invalid
        return [
            MeetingInvitee(
                user=eligible[(principals[user_id].user_id, principals[user_id].kind)],
                principal_kind=principals[user_id].kind,
                principal_id=principals[user_id].principal_id,
            )
            for user_id in deduped
        ]
