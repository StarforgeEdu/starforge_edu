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

_UPDATABLE = (
    "title",
    "description",
    "is_anonymous",
    "allow_multiple",
    "opens_at",
    "closes_at",
    "audience_roles",
    "audience_user_ids",
)


class FormService(IFormService):
    def __init__(self, forms: IFormRepository) -> None:
        self._forms = forms

    def scoped_list(
        self, *, user, is_unscoped: bool, can_write: bool, branch_ids: set[int]
    ) -> QuerySet[Form]:
        return self._forms.scoped(
            user=user, is_unscoped=is_unscoped, can_write=can_write, branch_ids=branch_ids
        )

    def get_visible(
        self, *, user, is_unscoped: bool, can_write: bool, branch_ids: set[int], pk: int
    ) -> Form | None:
        return self._forms.get_scoped(
            user=user, is_unscoped=is_unscoped, can_write=can_write, branch_ids=branch_ids, pk=pk
        )

    def create(self, data: CreateFormDTO, *, creator, is_unscoped: bool, branch_ids: set[int]) -> Form:
        from apps.forms.services import create_form

        branch = self._resolve_branch(data.branch_id)
        if not is_unscoped:
            # A non-director builds only within their own branch; only the director may
            # create a centre-wide (branch=None) form that reaches every branch.
            if branch is None:
                if len(branch_ids) == 1:
                    branch = self._branch_by_id(next(iter(branch_ids)))
                else:
                    raise ValidationException(_("Choose a branch for this form."), code="branch_required")
            elif branch.id not in branch_ids:
                raise PermissionException(
                    _("You can only create forms in your own branch."), code="cross_branch"
                )
        return create_form(
            title=data.title,
            created_by=creator,
            description=data.description,
            is_anonymous=data.is_anonymous,
            allow_multiple=data.allow_multiple,
            branch=branch,
            opens_at=data.opens_at,
            closes_at=data.closes_at,
            audience_roles=data.audience_roles,
            audience_user_ids=data.audience_user_ids,
        )

    def update(self, form: Form, changes: dict[str, Any]) -> Form:
        from apps.forms.services import update_form

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

    def submit(self, form: Form, *, respondent, answers: list[dict]) -> FormResponse:
        from apps.forms.services import submit_response

        return submit_response(form=form, respondent=respondent, answers=answers)

    def responses_of(self, form: Form) -> QuerySet[FormResponse]:
        return form.responses.select_related("respondent").prefetch_related("answers")

    def summary(self, form: Form) -> dict[str, Any]:
        from apps.forms.services import form_summary

        return form_summary(form)

    def analyze(self, form: Form, *, requested_by) -> Any:
        from apps.forms.services import request_form_analysis

        return request_form_analysis(form=form, requested_by=requested_by)

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

        return Branch.objects.filter(pk=branch_id).first()
