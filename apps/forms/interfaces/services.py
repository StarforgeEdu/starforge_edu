"""Forms-engine service port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.forms.dto.form_dto import AddFieldDTO, CreateFormDTO
from apps.forms.models import Form, FormField, FormResponse


class IFormService(ABC):
    @abstractmethod
    def scoped_list(
        self, *, user, is_unscoped: bool, can_write: bool, branch_ids: set[int]
    ) -> QuerySet[Form]: ...

    @abstractmethod
    def get_visible(
        self, *, user, is_unscoped: bool, can_write: bool, branch_ids: set[int], pk: int
    ) -> Form | None: ...

    @abstractmethod
    def create(self, data: CreateFormDTO, *, creator, is_unscoped: bool, branch_ids: set[int]) -> Form: ...

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
    def submit(self, form: Form, *, respondent, answers: list[dict]) -> FormResponse: ...

    @abstractmethod
    def responses_of(self, form: Form) -> QuerySet[FormResponse]: ...

    @abstractmethod
    def summary(self, form: Form) -> dict[str, Any]: ...

    @abstractmethod
    def analyze(self, form: Form, *, requested_by) -> Any: ...
