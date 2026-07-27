"""GuardianService — parent↔student links (create + delete; no update by design)."""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.parents.dto.parent_dto import GuardianCreateDTO
from apps.parents.interfaces.repositories import IGuardianRepository
from apps.parents.interfaces.services import IGuardianService
from apps.parents.models import Guardian
from core.exceptions import ValidationException


class GuardianService(IGuardianService):
    def __init__(self, guardians: IGuardianRepository) -> None:
        self._guardians = guardians

    def scoped_list(self, *, user, roles) -> QuerySet[Guardian]:
        return self._guardians.scoped(user=user, roles=roles)

    def get(self, *, user, roles, pk: int) -> Guardian | None:
        return self._guardians.get_scoped(user=user, roles=roles, pk=pk)

    def create(self, data: GuardianCreateDTO) -> Guardian:
        from apps.parents.services import link_guardian

        return link_guardian(
            parent=self._resolve_parent(data.parent_id),
            student=self._resolve_student(data.student_id),
            relationship=self._validate_relationship(data.relationship),
            is_primary=data.is_primary,
            custody_notes=data.custody_notes,
        )

    def delete(self, guardian: Guardian) -> None:
        self._guardians.delete(guardian)

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _validate_relationship(value: str) -> str:
        if value not in Guardian.Relationship.values:
            raise ValidationException(
                _("Invalid relationship."),
                code="validation_error",
                fields={"relationship": ["Not a valid choice."]},
            )
        return value

    @staticmethod
    def _resolve_parent(parent_id: int):
        from apps.parents.models import ParentProfile

        parent = ParentProfile.objects.filter(pk=parent_id).first()
        if parent is None:
            raise ValidationException(
                _("Invalid parent."), code="invalid_parent", fields={"parent": ["Not found."]}
            )
        return parent

    @staticmethod
    def _resolve_student(student_id: int):
        from apps.students.models import StudentProfile

        student = StudentProfile.objects.filter(pk=student_id).first()
        if student is None:
            raise ValidationException(
                _("Invalid student."), code="invalid_student", fields={"student": ["Not found."]}
            )
        return student
