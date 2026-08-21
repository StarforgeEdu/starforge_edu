"""DepartmentService — CRUD with the department-head-must-be-a-teacher guard."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DataError, IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.org.dto.org_dto import DepartmentCreateDTO
from apps.org.interfaces.repositories import IDepartmentRepository
from apps.org.interfaces.services import IDepartmentService
from apps.org.models import Department
from core.exceptions import NotFoundException, ValidationException

_SCALARS = ("name", "slug", "description", "is_active", "budget")


class DepartmentService(IDepartmentService):
    def __init__(self, departments: IDepartmentRepository) -> None:
        self._departments = departments

    def list(self) -> QuerySet[Department]:
        return self._departments.get_queryset()

    def get(self, department_id: int) -> Department | None:
        return self._departments.get_by_id(department_id)

    @transaction.atomic
    def create(self, data: DepartmentCreateDTO) -> Department:
        branch = self._resolve_branch(data.branch_id, for_update=True)
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

    @transaction.atomic
    def update(self, department: Department, changes: dict[str, Any]) -> Department:
        if "branch" in changes:
            raise ValidationException(
                _("A department cannot be moved with a generic update."),
                code="validation_error",
                fields={"branch": [_("This field is not supported.")]},
            )
        locked = (
            self._departments.get_queryset().select_for_update(of=("self",)).filter(pk=department.pk).first()
        )
        if locked is None:
            raise NotFoundException(code="not_found")
        department = locked
        if "head" in changes:
            department.head = self._resolve_head(changes["head"], branch_id=department.branch_id)
        for field in _SCALARS:
            if field in changes:
                setattr(department, field, changes[field])
        return self._save(department)

    @transaction.atomic
    def delete(self, department: Department) -> None:
        """Deactivate instead of cascading away memberships and history."""
        locked = (
            self._departments.get_queryset().select_for_update(of=("self",)).filter(pk=department.pk).first()
        )
        if locked is None:
            raise NotFoundException(code="not_found")
        if locked.is_active:
            locked.is_active = False
            locked.save(update_fields=["is_active", "updated_at"])

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
    def _resolve_branch(branch_id: int, *, for_update: bool = False):
        from apps.org.models import Branch

        queryset = Branch.objects.filter(is_active=True, archived_at__isnull=True)
        if for_update:
            queryset = queryset.select_for_update(of=("self",))
        branch = queryset.filter(pk=branch_id).first()
        if branch is None:
            raise ValidationException(
                _("Invalid branch."),
                code="invalid_branch",
                fields={"branch": [_("Choose an active branch.")]},
            )
        return branch

    @staticmethod
    def _save(department: Department) -> Department:
        try:
            department.full_clean(validate_unique=False, validate_constraints=False)
            with transaction.atomic():  # savepoint: unique-violation must not poison the txn
                department.save()
        except DjangoValidationError as exc:
            fields = {
                field: [str(message) for message in messages]
                for field, messages in getattr(exc, "message_dict", {"field": exc.messages}).items()
            }
            raise ValidationException(
                _("Please review the department fields."),
                code="validation_error",
                fields=fields,
            ) from exc
        except IntegrityError as exc:
            raise ValidationException(
                _("A department with this slug already exists in the branch."),
                code="validation_error",
                fields={"slug": ["Already used in this branch."]},
            ) from exc
        except DataError as exc:  # e.g. budget out of range -> clean 400, not a 500
            raise ValidationException(_("A field value is out of range."), code="validation_error") from exc
        return department
