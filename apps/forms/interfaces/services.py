"""Forms-engine service port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.forms.dto.form_dto import AddFieldDTO, CreateFormDTO
from apps.forms.models import Form, FormField, FormResponse
from core.role_principals import RolePrincipal


class IFormService(ABC):
    @abstractmethod
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
    ) -> QuerySet[Form]: ...

    @abstractmethod
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
    ) -> Form | None: ...

    @abstractmethod
    def create(
        self,
        data: CreateFormDTO,
        *,
        creator,
        creator_principal: RolePrincipal,
        is_unscoped: bool,
        branch_ids: set[int],
    ) -> Form: ...

    @abstractmethod
    def update(self, form: Form, changes: dict[str, Any]) -> Form: ...

    @abstractmethod
    def delete(self, form: Form) -> None: ...

    @abstractmethod
    def add_field(self, form: Form, data: AddFieldDTO) -> FormField: ...

    @abstractmethod
    def publish(self, form: Form) -> Form: ...

    @abstractmethod
    def close(self, form: Form) -> Form: ...

    @abstractmethod
    def submit(
        self,
        form: Form,
        *,
        respondent,
        respondent_principal_kind: str,
        respondent_principal_id: int,
        answers: list[dict],
    ) -> FormResponse: ...

    @abstractmethod
    def responses_of(self, form: Form) -> QuerySet[FormResponse]: ...

    @abstractmethod
    def summary(self, form: Form) -> dict[str, Any]: ...

    @abstractmethod
    def analyze(
        self,
        form: Form,
        *,
        requested_by,
        requested_principal: RolePrincipal,
    ) -> Any: ...
