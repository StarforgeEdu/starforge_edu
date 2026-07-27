"""DepartmentService — CRUD with the department-head-must-be-a-teacher guard."""

from __future__ import annotations

from typing import Any

from django.db import DataError, IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.org.dto.org_dto import DepartmentCreateDTO
from apps.org.interfaces.repositories import IDepartmentRepository
from apps.org.interfaces.services import IDepartmentService
from apps.org.models import Department
from core.exceptions import ValidationException

_SCALARS = ("name", "slug", "description", "is_active", "budget")


class DepartmentService(IDepartmentService):
    def __init__(self, departments: IDepartmentRepository) -> None:
        self._departments = departments

    def list(self) -> QuerySet[Department]:
        return self._departments.get_queryset()

    def get(self, department_id: int) -> Department | None:
        return self._departments.get_by_id(department_id)

    def create(self, data: DepartmentCreateDTO) -> Department:
        branch = self._resolve_branch(data.branch_id)
        dept = Department(
            branch=branch,
            name=data.name,
            slug=data.slug,
            description=data.description,
            is_active=data.is_active,
            head=self._resolve_head(data.head_id, branch_id=branch.pk),
            budget=data.budget,
        )
        return self._save(dept)

    def update(self, department: Department, changes: dict[str, Any]) -> Department:
        if "branch" in changes or "head" in changes:
            branch = self._resolve_branch(changes["branch"]) if "branch" in changes else department.branch
            head_id = changes.get("head", department.head_id)
            department.branch = branch
            department.head = self._resolve_head(head_id, branch_id=branch.pk)
        for field in _SCALARS:
            if field in changes:
                setattr(department, field, changes[field])
        return self._save(department)

    def delete(self, department: Department) -> None:
        self._departments.delete(department)

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _resolve_head(head_id: int | None, *, branch_id: int):
        from apps.org.services import validate_department_head

        if head_id is None:
            validate_department_head(None)  # clearing is always allowed
            return None
        from apps.teachers.models import TeacherProfile

        teacher = TeacherProfile.objects.select_related("user").filter(pk=head_id).first()
        if teacher is None:
            raise ValidationException(
                _("Invalid head."), code="invalid_head", fields={"head": ["Not found."]}
            )
        validate_department_head(teacher, branch_id=branch_id)
        return teacher.user

    @staticmethod
    def _resolve_branch(branch_id: int):
        from apps.org.models import Branch

        branch = Branch.objects.filter(pk=branch_id).first()
        if branch is None:
            raise ValidationException(
                _("Invalid branch."), code="invalid_branch", fields={"branch": ["Not found."]}
            )
        return branch

    @staticmethod
    def _save(department: Department) -> Department:
        try:
            with transaction.atomic():  # savepoint: unique-violation must not poison the txn
                department.save()
        except IntegrityError as exc:
            raise ValidationException(
                _("A department with this slug already exists in the branch."),
                code="validation_error",
                fields={"slug": ["Already used in this branch."]},
            ) from exc
        except DataError as exc:  # e.g. budget out of range -> clean 400, not a 500
            raise ValidationException(_("A field value is out of range."), code="validation_error") from exc
        return department
