"""FormService — the layered facade over the forms-engine domain functions.

Read scoping is delegated to the repository; the build/publish/submit/summarize/analyze
lifecycle routes through the transactional domain functions in ``apps.forms.services``.
Branch containment on create (a non-director may only build in their own branch, and
only the director may make a centre-wide form) lives here since it needs the resolved
branch + the caller's scope.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.forms.dto.form_dto import AddFieldDTO, CreateFormDTO
from apps.forms.interfaces.repositories import IFormRepository
from apps.forms.interfaces.services import IFormService
from apps.forms.models import Form, FormField, FormResponse
from core.exceptions import PermissionException, ValidationException
from core.role_principals import RolePrincipal, resolve_unambiguous_user_principals

_UPDATABLE = (
    "title",
    "description",
    "is_anonymous",
    "allow_multiple",
    "opens_at",
    "closes_at",
    "audience_roles",
    "audience_user_ids",
    "audience_principals",
)


class FormService(IFormService):
    def __init__(self, forms: IFormRepository) -> None:
        self._forms = forms

    def scoped_list(
        self,
        *,
        user,
        read_unscoped: bool,
        write_unscoped: bool,
        can_write: bool,
        roles: set[str],
        principal_kind: str,
        principal_id: int,
        read_branch_ids: set[int],
        write_branch_ids: set[int],
    ) -> QuerySet[Form]:
        return self._forms.scoped(
            user=user,
            read_unscoped=read_unscoped,
            write_unscoped=write_unscoped,
            can_write=can_write,
            roles=roles,
            principal_kind=principal_kind,
            principal_id=principal_id,
            read_branch_ids=read_branch_ids,
            write_branch_ids=write_branch_ids,
        )

    def get_visible(
        self,
        *,
        user,
        read_unscoped: bool,
        write_unscoped: bool,
        can_write: bool,
        roles: set[str],
        principal_kind: str,
        principal_id: int,
        read_branch_ids: set[int],
        write_branch_ids: set[int],
        pk: int,
    ) -> Form | None:
        return self._forms.get_scoped(
            user=user,
            read_unscoped=read_unscoped,
            write_unscoped=write_unscoped,
            can_write=can_write,
            roles=roles,
            principal_kind=principal_kind,
            principal_id=principal_id,
            read_branch_ids=read_branch_ids,
            write_branch_ids=write_branch_ids,
            pk=pk,
        )

    def create(
        self,
        data: CreateFormDTO,
        *,
        creator,
        creator_principal: RolePrincipal,
        is_unscoped: bool,
        branch_ids: set[int],
    ) -> Form:
        from apps.forms.services import create_form

        if not is_unscoped and data.branch_id is not None and data.branch_id not in branch_ids:
            # Reject outside the permission boundary before querying Branch so a
            # caller cannot distinguish another branch from a nonexistent id.
            raise PermissionException(_("You can only create forms in your own branch."), code="cross_branch")
        branch = self._resolve_branch(data.branch_id)
        if not is_unscoped:
            # A non-director builds only within their own branch; only the director may
            # create a centre-wide (branch=None) form that reaches every branch.
            if branch is None:
                if len(branch_ids) == 1:
                    branch = self._branch_by_id(next(iter(branch_ids)))
                    if branch is None:
                        raise ValidationException(
                            _("Choose an active branch for this form."),
                            code="branch_required",
                            fields={"branch": ["The membership branch is archived."]},
                        )
                else:
                    raise ValidationException(_("Choose a branch for this form."), code="branch_required")
            elif branch.id not in branch_ids:
                raise PermissionException(
                    _("You can only create forms in your own branch."), code="cross_branch"
                )
        audience_principals = self._resolve_audience_principals(
            data.audience_user_ids,
            branch_id=branch.pk if branch is not None else None,
        )
        return create_form(
            title=data.title,
            created_by=creator,
            created_by_principal=creator_principal,
            description=data.description,
            is_anonymous=data.is_anonymous,
            allow_multiple=data.allow_multiple,
            branch=branch,
            opens_at=data.opens_at,
            closes_at=data.closes_at,
            audience_roles=data.audience_roles,
            audience_user_ids=data.audience_user_ids,
            audience_principals=audience_principals,
        )

    def update(self, form: Form, changes: dict[str, Any]) -> Form:
        from apps.forms.services import update_form

        if "audience_user_ids" in changes:
            changes = {
                **changes,
                "audience_principals": self._resolve_audience_principals(
                    changes["audience_user_ids"],
                    branch_id=form.branch_id,
                ),
            }
        return update_form(form=form, **{k: v for k, v in changes.items() if k in _UPDATABLE})

    def delete(self, form: Form) -> None:
        from apps.forms.services import delete_form

        delete_form(form=form)

    def add_field(self, form: Form, data: AddFieldDTO) -> FormField:
        from apps.forms.services import add_field

        return add_field(
            form=form,
            label=data.label,
            field_type=data.field_type,
            required=data.required,
            order=data.order,
            options=data.options,
            help_text=data.help_text,
        )

    def publish(self, form: Form) -> Form:
        from apps.forms.services import publish_form

        return publish_form(form=form)

    def close(self, form: Form) -> Form:
        from apps.forms.services import close_form

        return close_form(form=form)

    def submit(
        self,
        form: Form,
        *,
        respondent,
        respondent_principal_kind: str,
        respondent_principal_id: int,
        answers: list[dict],
    ) -> FormResponse:
        from apps.forms.services import submit_response

        return submit_response(
            form=form,
            respondent=respondent,
            respondent_principal_kind=respondent_principal_kind,
            respondent_principal_id=respondent_principal_id,
            answers=answers,
        )

    def responses_of(self, form: Form) -> QuerySet[FormResponse]:
        return form.responses.select_related("respondent").prefetch_related("answers")

    def summary(self, form: Form) -> dict[str, Any]:
        from apps.forms.services import form_summary

        return form_summary(form)

    def analyze(
        self,
        form: Form,
        *,
        requested_by,
        requested_principal: RolePrincipal,
    ) -> Any:
        from apps.forms.services import request_form_analysis

        return request_form_analysis(
            form=form,
            requested_by=requested_by,
            requested_principal=requested_principal,
        )

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _resolve_branch(branch_id: int | None):
        if branch_id is None:
            return None
        from apps.org.models import Branch

        branch = Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
        if branch is None:  # mirrors the old serializer's non-archived branch queryset
            raise ValidationException(
                _("Invalid branch."), code="validation_error", fields={"branch": ["Not found."]}
            )
        return branch

    @staticmethod
    def _branch_by_id(branch_id: int):
        from apps.org.models import Branch

        return Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()

    @staticmethod
    def _resolve_audience_principals(user_ids: list[int], *, branch_id: int | None) -> list[dict[str, Any]]:
        if not user_ids:
            return []
        from apps.access.models import AccountType
        from core.permissions import Role, role_memberships_with_permission

        memberships = role_memberships_with_permission("forms:read").filter(user_id__in=user_ids)
        if branch_id is not None:
            memberships = memberships.filter(branch_id=branch_id)
        legacy_kinds = {
            Role.STUDENT: AccountType.AccountKind.STUDENT,
            Role.TEACHER: AccountType.AccountKind.TEACHER,
            Role.PARENT: AccountType.AccountKind.PARENT,
        }
        eligible: set[tuple[int, str]] = set()
        for membership in memberships.select_related("account_type"):
            kind = (
                membership.account_type.account_kind
                if membership.account_type_id is not None
                else legacy_kinds.get(membership.role, AccountType.AccountKind.STAFF)
            )
            eligible.add((membership.user_id, str(kind)))
        invalid = ValidationException(
            _("One or more audience users cannot read forms in the selected scope."),
            code="validation_error",
            fields={"audience_user_ids": [_("Choose active recipients in the form's scope.")]},
        )
        if not set(user_ids).issubset({user_id for user_id, _kind in eligible}):
            raise invalid
        try:
            principals = resolve_unambiguous_user_principals(
                user_ids,
                field="audience_user_ids",
                message=_("Choose active recipients in the form's scope."),
            )
        except ValidationException:
            raise invalid from None
        if any((principal.user_id, principal.kind) not in eligible for principal in principals.values()):
            raise invalid
        return [
            {"kind": principal.kind, "id": principal.principal_id, "user_id": principal.user_id}
            for user_id in user_ids
            for principal in (principals[user_id],)
        ]
