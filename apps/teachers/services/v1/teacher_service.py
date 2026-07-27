"""TeacherService — ITeacherService impl. Repo-injected orchestration; reuses the
tested create_teacher domain fn and adds update/delete with the branch↔department guard."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.teachers.dto.teacher_dto import TeacherCreateDTO
from apps.teachers.interfaces.repositories import ITeacherRepository
from apps.teachers.interfaces.teacher_service import ITeacherService
from apps.teachers.models import TeacherProfile
from core.exceptions import NotFoundException, ValidationException

_SCALAR_FIELDS = ("hire_date", "subjects", "qualifications", "salary_type", "rate", "is_substitute")
_IDENTITY_FIELDS = (
    "first_name",
    "last_name",
    "middle_name",
    "phone",
    "email",
    "birthdate",
    "gender",
    "is_active",
)


class TeacherService(ITeacherService):
    def __init__(self, teachers: ITeacherRepository) -> None:
        self._teachers = teachers

    def list(self) -> QuerySet[TeacherProfile]:
        return self._teachers.get_queryset()

    def get(self, teacher_id: int) -> TeacherProfile | None:
        return self._teachers.get_by_id(teacher_id)

    def create(self, data: TeacherCreateDTO) -> TeacherProfile:
        from apps.teachers.services import create_teacher

        return create_teacher(
            branch=self._resolve_branch(data.branch_id),
            department=self._resolve_department(data.department_id),
            username=data.username,
            phone=data.phone,
            email=data.email,
            first_name=data.first_name,
            last_name=data.last_name,
            middle_name=data.middle_name,
            birthdate=data.birthdate,
            gender=data.gender,
            hire_date=data.hire_date,
            subjects=data.subjects,
            qualifications=data.qualifications,
            salary_type=data.salary_type,
            rate=data.rate,
            is_substitute=data.is_substitute,
        )

    def update(self, teacher: TeacherProfile, changes: dict[str, Any]) -> TeacherProfile:
        identity_changes = {field: changes[field] for field in _IDENTITY_FIELDS if field in changes}
        if identity_changes:
            from apps.users.services import update_role_identity

            update_role_identity(teacher, identity_changes)
        if "branch" in changes:
            teacher.branch = self._resolve_branch(changes["branch"])
        if "department" in changes:
            dep_id = changes["department"]
            teacher.department = self._resolve_department(dep_id) if dep_id is not None else None
        # Cross-field guard (mirrors create): a non-null department must belong to the
        # teacher's (possibly just-changed) branch — covers both change directions.
        if (
            teacher.department is not None
            and teacher.branch is not None
            and teacher.department.branch_id != teacher.branch_id
        ):
            raise ValidationException(
                _("Department must belong to the teacher's branch."),
                code="department_branch_mismatch",
                fields={"department": ["Department must belong to the teacher's branch."]},
            )
        for field in _SCALAR_FIELDS:
            if field in changes:
                setattr(teacher, field, changes[field])
        teacher.save()
        from apps.users.services import ensure_role_membership
        from core.permissions import Role

        ensure_role_membership(
            teacher,
            role=Role.TEACHER,
            branch=teacher.branch,
            department=teacher.department,
        )
        return teacher

    def delete(self, teacher: TeacherProfile) -> None:
        self._teachers.delete(teacher)

    def dashboard(self, user, roles) -> dict[str, Any]:
        from apps.teachers.selectors import teacher_dashboard, teacher_profile_for

        teacher = teacher_profile_for(user)
        if teacher is None:
            raise NotFoundException(_("You do not have a teacher profile."), code="not_a_teacher")
        return teacher_dashboard(teacher=teacher, user=user, roles=roles)

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
    def _resolve_department(department_id: int | None):
        if department_id is None:
            return None
        from apps.org.models import Department

        dept = Department.objects.filter(pk=department_id).first()
        if dept is None:
            raise ValidationException(
                _("Invalid department."),
                code="invalid_department",
                fields={"department": ["Not found."]},
            )
        return dept
