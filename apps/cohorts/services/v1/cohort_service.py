"""CohortService — ICohortService impl.

Repo-injected orchestration. CRUD lives here (with the archived/history guards the
old viewset enforced); enroll/move delegate to the tested module-level domain
functions so the finance auto-issue signal and history invariants are unchanged.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.cohorts.dto.cohort_dto import (
    CohortCreateDTO,
    CohortEnrollDTO,
    CohortMoveDTO,
    CohortRemoveDTO,
    CohortTeacherDTO,
)
from apps.cohorts.interfaces.cohort_service import ICohortService
from apps.cohorts.interfaces.repositories import ICohortRepository
from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher
from core.exceptions import ConflictException, ValidationException

_SCALAR_FIELDS = ("name", "level", "start_date", "end_date", "capacity", "is_archived")


class CohortService(ICohortService):
    def __init__(self, cohorts: ICohortRepository) -> None:
        self._cohorts = cohorts

    def list(self) -> QuerySet[Cohort]:
        return self._cohorts.get_queryset()

    def get(self, cohort_id: int) -> Cohort | None:
        return self._cohorts.get_by_id(cohort_id)

    def create(self, data: CohortCreateDTO) -> Cohort:
        self._assert_date_order(data.start_date, data.end_date)
        cohort = Cohort(
            name=data.name,
            branch=self._resolve_branch(data.branch_id),
            department=self._resolve_department(data.department_id),
            level=data.level,
            start_date=data.start_date,
            end_date=data.end_date,
            capacity=data.capacity,
            primary_teacher=self._resolve_teacher(data.primary_teacher_id),
            default_room=self._resolve_room(data.default_room_id),
            is_archived=data.is_archived,
        )
        self._validate_cohort(cohort)
        return self._save(cohort)

    def update(self, cohort: Cohort, changes: dict[str, Any]) -> Cohort:
        if cohort.is_archived:
            raise ValidationException(_("Cohort is archived."), code="cohort_archived")
        if "branch" in changes:
            cohort.branch = self._resolve_branch(changes["branch"])
        if "department" in changes:
            dep_id = changes["department"]
            cohort.department = self._resolve_department(dep_id) if dep_id is not None else None
        if "primary_teacher" in changes:
            t_id = changes["primary_teacher"]
            cohort.primary_teacher = self._resolve_teacher(t_id) if t_id is not None else None
        if "default_room" in changes:
            r_id = changes["default_room"]
            cohort.default_room = self._resolve_room(r_id) if r_id is not None else None
        for field in _SCALAR_FIELDS:
            if field in changes:
                setattr(cohort, field, changes[field])
        self._validate_cohort(cohort)
        return self._save(cohort)

    def delete(self, cohort: Cohort) -> None:
        # Membership history is never cascaded away (D1-LD-9); archived cohorts are
        # read-only, and a cohort with any history is archived, not deleted.
        if cohort.is_archived:
            raise ValidationException(_("Cohort is archived."), code="cohort_archived")
        if self._cohorts.has_memberships(cohort):
            raise ConflictException(
                _("Cohort has membership history; archive it instead of deleting."),
                code="cohort_has_history",
            )
        self._cohorts.delete(cohort)

    def unarchive(self, cohort: Cohort) -> Cohort:
        cohort.is_archived = False
        cohort.save(update_fields=["is_archived", "updated_at"])
        return cohort

    def enroll(self, cohort: Cohort, data: CohortEnrollDTO) -> CohortMembership:
        from apps.cohorts.services import enroll_student_in_cohort

        return enroll_student_in_cohort(
            cohort=cohort, student=self._resolve_student(data.student_id), start_date=data.start_date
        )

    def move(self, cohort: Cohort, data: CohortMoveDTO, actor) -> dict[str, Any]:
        from apps.cohorts.services import move_student

        return move_student(
            student=self._resolve_student(data.student_id),
            to_cohort=cohort,
            reason=data.reason,
            actor=actor,
        )

    def remove_member(self, cohort: Cohort, data: CohortRemoveDTO, actor) -> CohortMembership:
        from apps.cohorts.services import unenroll_student_from_cohort

        return unenroll_student_from_cohort(
            cohort=cohort, student=self._resolve_student(data.student_id), reason=data.reason
        )

    def members(self, cohort: Cohort) -> QuerySet[CohortMembership]:
        return self._cohorts.active_members(cohort)

    def co_teachers(self, cohort: Cohort) -> QuerySet[CohortTeacher]:
        return CohortTeacher.objects.filter(cohort=cohort).order_by("teacher_id")

    def assign_teacher(self, cohort: Cohort, data: CohortTeacherDTO) -> tuple[CohortTeacher, bool]:
        from apps.cohorts.services import assign_cohort_teacher

        teacher = self._resolve_teacher(data.teacher_id)  # 400 if not found
        if teacher.branch_id != cohort.branch_id:
            raise ValidationException(
                _("The teacher must belong to the cohort's branch."),
                code="cross_branch_relationship",
                fields={"teacher": ["Must belong to the cohort's branch."]},
            )
        return assign_cohort_teacher(cohort=cohort, teacher=teacher, role=data.role)

    def remove_teacher(self, cohort: Cohort, teacher_id: int) -> None:
        from apps.cohorts.services import remove_cohort_teacher

        remove_cohort_teacher(cohort=cohort, teacher_id=teacher_id)

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _assert_date_order(start, end) -> None:
        if start and end and start > end:
            raise ValidationException(
                _("end_date must be on or after start_date."),
                code="validation_error",
                fields={"end_date": ["Must be on or after start_date."]},
            )

    @classmethod
    def _validate_cohort(cls, cohort: Cohort) -> None:
        name = str(cohort.name or "").strip()
        if not name:
            raise ValidationException(
                _("Name is required."),
                code="validation_error",
                fields={"name": ["This field may not be blank."]},
            )
        cohort.name = name
        if cohort.capacity is not None and cohort.capacity < 0:
            raise ValidationException(
                _("Capacity cannot be negative."),
                code="validation_error",
                fields={"capacity": ["Must be zero or greater."]},
            )
        cls._assert_date_order(cohort.start_date, cohort.end_date)

        mismatches: dict[str, list[str]] = {}
        for field_name in ("department", "primary_teacher", "default_room"):
            related = getattr(cohort, field_name)
            if related is not None and related.branch_id != cohort.branch_id:
                mismatches[field_name] = ["Must belong to the cohort's branch."]
        if mismatches:
            raise ValidationException(
                _("Cohort relationships must belong to the same branch."),
                code="cross_branch_relationship",
                fields=mismatches,
            )

    def _save(self, cohort: Cohort) -> Cohort:
        try:
            cohort.save()
        except IntegrityError as exc:
            # unique_together (branch, name) collided — a duplicate cohort name in the
            # branch. Surface as 409, never a 500.
            raise ConflictException(
                _("A cohort with this name already exists in this branch."),
                code="cohort_name_taken",
                fields={"name": ["Already used in this branch."]},
            ) from exc
        return cohort

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

    @staticmethod
    def _resolve_teacher(teacher_id: int | None):
        if teacher_id is None:
            return None
        from apps.teachers.models import TeacherProfile

        teacher = TeacherProfile.objects.filter(pk=teacher_id).first()
        if teacher is None:
            raise ValidationException(
                _("Invalid teacher."),
                code="invalid_teacher",
                fields={"primary_teacher": ["Not found."]},
            )
        return teacher

    @staticmethod
    def _resolve_room(room_id: int | None):
        if room_id is None:
            return None
        from apps.org.models import Room

        room = Room.objects.filter(pk=room_id).first()
        if room is None:
            raise ValidationException(
                _("Invalid room."), code="invalid_room", fields={"default_room": ["Not found."]}
            )
        return room

    @staticmethod
    def _resolve_student(student_id: int):
        from apps.students.models import StudentProfile

        student = StudentProfile.objects.filter(pk=student_id).first()
        if student is None:
            raise ValidationException(
                _("Invalid student."), code="invalid_student", fields={"student": ["Not found."]}
            )
        return student
