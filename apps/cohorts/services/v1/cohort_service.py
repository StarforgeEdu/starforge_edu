"""CohortService — ICohortService impl.

Repo-injected orchestration. CRUD lives here (with the archived/history guards the
old viewset enforced); enroll/move delegate to the tested module-level domain
functions so the finance auto-issue signal and history invariants are unchanged.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.db.models.deletion import ProtectedError
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.cohorts.dto.cohort_dto import (
    CohortCreateDTO,
    CohortEnrollDTO,
    CohortMoveDTO,
    CohortRemoveDTO,
    CohortTeacherDTO,
    TeacherTypeCreateDTO,
)
from apps.cohorts.interfaces.cohort_service import ICohortService
from apps.cohorts.interfaces.repositories import ICohortRepository
from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher
from apps.cohorts.teacher_assignments import (
    default_teacher_type,
    refresh_primary_teacher,
    resolve_assignment_type,
    type_slug_for_legacy_role,
)
from apps.teachers.models import TeacherType
from core.exceptions import ConflictException, ValidationException

_SCALAR_FIELDS = (
    "name",
    "level",
    "study_month",
    "lesson_cycle_length",
    "start_date",
    "end_date",
    "capacity",
    "is_archived",
)


class CohortService(ICohortService):
    def __init__(self, cohorts: ICohortRepository) -> None:
        self._cohorts = cohorts

    def list(self) -> QuerySet[Cohort]:
        return self._cohorts.get_queryset()

    def get(self, cohort_id: int) -> Cohort | None:
        return self._cohorts.get_by_id(cohort_id)

    @transaction.atomic
    def create(self, data: CohortCreateDTO) -> Cohort:
        self._assert_date_order(data.start_date, data.end_date)
        cohort = Cohort(
            name=data.name,
            branch=self._resolve_branch(data.branch_id),
            department=self._resolve_department(data.department_id),
            level=data.level,
            study_month=data.study_month,
            lesson_cycle_length=data.lesson_cycle_length,
            start_date=data.start_date,
            end_date=data.end_date,
            capacity=data.capacity,
            primary_teacher=self._resolve_teacher(data.primary_teacher_id),
            default_room=self._resolve_room(data.default_room_id),
            is_archived=data.is_archived,
        )
        self._validate_cohort(cohort)
        return self._save(cohort)

    @transaction.atomic
    def update(self, cohort: Cohort, changes: dict[str, Any]) -> Cohort:
        if cohort.is_archived:
            raise ValidationException(_("Cohort is archived."), code="cohort_archived")
        previous_primary_teacher_id = cohort.primary_teacher_id
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
        ignore_legacy_main = (
            previous_primary_teacher_id
            if "primary_teacher" in changes and previous_primary_teacher_id != cohort.primary_teacher_id
            else None
        )
        self._validate_existing_assignments(cohort, ignore_main_teacher_id=ignore_legacy_main)
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

    def teacher_types(self) -> QuerySet[TeacherType]:
        return self._cohorts.teacher_types()

    def get_teacher_type(self, teacher_type_id: int) -> TeacherType | None:
        return self._cohorts.get_teacher_type(teacher_type_id)

    @transaction.atomic
    def create_teacher_type(self, data: TeacherTypeCreateDTO) -> TeacherType:
        name = self._teacher_type_name(data.name)
        type_slug = self._teacher_type_slug(data.slug, name=name)
        self._validate_teacher_type_state(
            is_active=data.is_active,
            is_default=data.is_default,
            sort_order=data.sort_order,
        )
        self._assert_teacher_type_identity_free(name=name, type_slug=type_slug)
        if data.is_default:
            TeacherType.objects.select_for_update().filter(is_default=True).update(is_default=False)
        try:
            return TeacherType.objects.create(
                name=name,
                slug=type_slug,
                description=data.description.strip(),
                is_active=data.is_active,
                is_system=False,
                is_default=data.is_default,
                sort_order=data.sort_order,
            )
        except IntegrityError as exc:
            raise ConflictException(
                _("A teacher type with this name or slug already exists."),
                code="teacher_type_exists",
            ) from exc

    @transaction.atomic
    def update_teacher_type(self, teacher_type: TeacherType, changes: dict[str, Any]) -> TeacherType:
        if teacher_type.is_system:
            for field in ("name", "slug"):
                if field in changes and changes[field] != getattr(teacher_type, field):
                    raise ValidationException(
                        _("System teacher type identity cannot be changed."),
                        code="system_teacher_type_immutable",
                        fields={field: ["System teacher types cannot be renamed."]},
                    )

        name = self._teacher_type_name(changes.get("name", teacher_type.name))
        type_slug = self._teacher_type_slug(changes.get("slug", teacher_type.slug), name=name)
        is_active = changes.get("is_active", teacher_type.is_active)
        is_default = changes.get("is_default", teacher_type.is_default)
        sort_order = changes.get("sort_order", teacher_type.sort_order)
        if teacher_type.is_system and not is_active:
            raise ValidationException(
                _("System teacher types cannot be deactivated."),
                code="system_teacher_type_immutable",
                fields={"is_active": ["System teacher types must remain active."]},
            )
        if teacher_type.is_default and not is_default:
            raise ValidationException(
                _("A default teacher type is required."),
                code="default_teacher_type_required",
                fields={"is_default": ["Choose another default before clearing this one."]},
            )
        self._validate_teacher_type_state(is_active=is_active, is_default=is_default, sort_order=sort_order)
        self._assert_teacher_type_identity_free(name=name, type_slug=type_slug, exclude_pk=teacher_type.pk)
        if is_default and not teacher_type.is_default:
            TeacherType.objects.select_for_update().exclude(pk=teacher_type.pk).filter(
                is_default=True
            ).update(is_default=False)
        for field, value in {
            "name": name,
            "slug": type_slug,
            "description": changes.get("description", teacher_type.description).strip(),
            "is_active": is_active,
            "is_default": is_default,
            "sort_order": sort_order,
        }.items():
            setattr(teacher_type, field, value)
        try:
            teacher_type.save()
        except IntegrityError as exc:
            raise ConflictException(
                _("A teacher type with this name or slug already exists."),
                code="teacher_type_exists",
            ) from exc
        return teacher_type

    def delete_teacher_type(self, teacher_type: TeacherType) -> None:
        if teacher_type.is_system:
            raise ValidationException(
                _("System teacher types cannot be deleted."),
                code="system_teacher_type_immutable",
            )
        if teacher_type.is_default:
            raise ValidationException(
                _("The default teacher type cannot be deleted."),
                code="default_teacher_type_required",
            )
        try:
            teacher_type.delete()
        except ProtectedError as exc:
            raise ConflictException(
                _("This teacher type is still used by cohort assignments."),
                code="teacher_type_in_use",
            ) from exc

    def co_teachers(self, cohort: Cohort) -> QuerySet[CohortTeacher]:
        return self._cohorts.teacher_assignments(cohort)

    def get_teacher_assignment(self, cohort: Cohort, assignment_id: int) -> CohortTeacher | None:
        return self._cohorts.get_teacher_assignment(cohort, assignment_id)

    def assign_teacher(self, cohort: Cohort, data: CohortTeacherDTO) -> tuple[CohortTeacher, bool]:
        self._assert_cohort_writable(cohort)
        teacher = self._resolve_teacher(data.teacher_id)  # 400 if not found
        teacher_type = self._resolve_assignment_type(
            teacher_type_id=data.teacher_type_id, legacy_role=data.legacy_role
        )
        self._assert_teacher_consistency(cohort, teacher)
        try:
            return CohortTeacher.objects.get_or_create(
                cohort=cohort, teacher=teacher, teacher_type=teacher_type
            )
        except IntegrityError:
            # The database constraint wins a concurrent duplicate race.
            existing = CohortTeacher.objects.get(cohort=cohort, teacher=teacher, teacher_type=teacher_type)
            return existing, False

    def update_teacher_assignment(
        self, cohort: Cohort, assignment: CohortTeacher, changes: dict[str, Any]
    ) -> CohortTeacher:
        self._assert_cohort_writable(cohort)
        if not changes:
            return assignment
        teacher = self._resolve_teacher(changes["teacher"]) if "teacher" in changes else assignment.teacher
        if "teacher_type" in changes or "legacy_role" in changes:
            teacher_type = self._resolve_assignment_type(
                teacher_type_id=changes.get("teacher_type"),
                legacy_role=changes.get("legacy_role", ""),
            )
        else:
            resolved_type = resolve_assignment_type(assignment)
            if resolved_type is None:
                raise ValidationException(
                    _("This legacy assignment has no valid teacher type."),
                    code="invalid_teacher_type",
                    fields={"teacher_type": ["Not found."]},
                )
            teacher_type = resolved_type
            if not teacher_type.is_active:
                raise ValidationException(
                    _("Inactive teacher types cannot be assigned."),
                    code="inactive_teacher_type",
                    fields={"teacher_type": ["Choose an active teacher type."]},
                )
        self._assert_teacher_consistency(cohort, teacher)
        duplicate = CohortTeacher.objects.filter(
            cohort=cohort, teacher=teacher, teacher_type=teacher_type
        ).exclude(pk=assignment.pk)
        if duplicate.exists():
            raise ConflictException(
                _("This teacher already has that type in the cohort."),
                code="teacher_assignment_exists",
            )
        assignment.teacher = teacher
        assignment.teacher_type = teacher_type
        try:
            assignment.save(update_fields=["teacher", "teacher_type"])
        except IntegrityError as exc:
            raise ConflictException(
                _("This teacher already has that type in the cohort."),
                code="teacher_assignment_exists",
            ) from exc
        refresh_primary_teacher(cohort)
        return assignment

    def remove_teacher(self, cohort: Cohort, assignment: CohortTeacher) -> None:
        self._assert_cohort_writable(cohort)
        assignment.delete()
        refresh_primary_teacher(cohort)

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
        if cohort.lesson_cycle_length not in Cohort.LessonCycleLength.values:
            raise ValidationException(
                _("Lesson cycle must contain 8 or 12 lessons."),
                code="validation_error",
                fields={"lesson_cycle_length": ["Choose 8 or 12 lessons."]},
            )
        if not 1 <= cohort.study_month <= 600:
            raise ValidationException(
                _("Study month must be between 1 and 600."),
                code="validation_error",
                fields={"study_month": ["Choose a value from 1 to 600."]},
            )
        cls._assert_date_order(cohort.start_date, cohort.end_date)

        mismatches: dict[str, list[str]] = {}
        for field_name in ("department", "primary_teacher", "default_room"):
            related = getattr(cohort, field_name)
            if related is not None and related.branch_id != cohort.branch_id:
                mismatches[field_name] = ["Must belong to the cohort's branch."]
        primary_teacher = cohort.primary_teacher
        if cohort.department_id and primary_teacher and primary_teacher.department_id != cohort.department_id:
            mismatches["primary_teacher"] = ["Must belong to the cohort's department."]
        if mismatches:
            raise ValidationException(
                _("Cohort relationships must belong to the same branch."),
                code="cross_branch_relationship",
                fields=mismatches,
            )

    @classmethod
    def _validate_existing_assignments(
        cls, cohort: Cohort, *, ignore_main_teacher_id: int | None = None
    ) -> None:
        assignments = cohort.co_teachers.select_related("teacher", "teacher_type")
        for assignment in assignments:
            teacher_type = resolve_assignment_type(assignment)
            if (
                ignore_main_teacher_id == assignment.teacher_id
                and teacher_type is not None
                and teacher_type.slug == "main-teacher"
            ):
                continue
            cls._assert_teacher_consistency(cohort, assignment.teacher)

    @staticmethod
    def _assert_cohort_writable(cohort: Cohort) -> None:
        if cohort.is_archived:
            raise ValidationException(_("Cohort is archived."), code="cohort_archived")

    @staticmethod
    def _assert_teacher_consistency(cohort: Cohort, teacher) -> None:
        if teacher.branch_id != cohort.branch_id:
            raise ValidationException(
                _("The teacher must belong to the cohort's branch."),
                code="cross_branch_relationship",
                fields={"teacher": ["Must belong to the cohort's branch."]},
            )
        if cohort.department_id and teacher.department_id != cohort.department_id:
            raise ValidationException(
                _("The teacher must belong to the cohort's department."),
                code="cross_department_relationship",
                fields={"teacher": ["Must belong to the cohort's department."]},
            )

    def _resolve_assignment_type(self, *, teacher_type_id: int | None, legacy_role: str) -> TeacherType:
        if teacher_type_id is not None:
            teacher_type = self._cohorts.get_teacher_type(teacher_type_id)
            field = "teacher_type"
        elif legacy_role:
            type_slug = type_slug_for_legacy_role(legacy_role)
            if type_slug is None:
                raise ValidationException(
                    _("Invalid teaching role."),
                    code="validation_error",
                    fields={"role": ["Unknown legacy teaching role."]},
                )
            teacher_type = TeacherType.objects.filter(slug=type_slug).first()
            field = "role"
        else:
            teacher_type = default_teacher_type()
            field = "teacher_type"
        if teacher_type is None:
            raise ValidationException(
                _("Invalid teacher type."),
                code="invalid_teacher_type",
                fields={field: ["Not found."]},
            )
        if not teacher_type.is_active:
            raise ValidationException(
                _("Inactive teacher types cannot be assigned."),
                code="inactive_teacher_type",
                fields={"teacher_type": ["Choose an active teacher type."]},
            )
        return teacher_type

    @staticmethod
    def _teacher_type_name(value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValidationException(
                _("Teacher type name is required."),
                code="validation_error",
                fields={"name": ["This field is required."]},
            )
        return name

    @staticmethod
    def _teacher_type_slug(value: str, *, name: str) -> str:
        type_slug = slugify(value or name)
        if not type_slug:
            raise ValidationException(
                _("A valid teacher type slug is required."),
                code="validation_error",
                fields={"slug": ["Use letters, numbers, or hyphens."]},
            )
        if len(type_slug) > 80:
            raise ValidationException(
                _("Teacher type slug is too long."),
                code="validation_error",
                fields={"slug": ["Must be at most 80 characters."]},
            )
        return type_slug

    @staticmethod
    def _validate_teacher_type_state(*, is_active: bool, is_default: bool, sort_order: int) -> None:
        if is_default and not is_active:
            raise ValidationException(
                _("The default teacher type must be active."),
                code="inactive_teacher_type",
                fields={"is_active": ["A default type must be active."]},
            )
        if sort_order < 0 or sort_order > 32767:
            raise ValidationException(
                _("Invalid teacher type order."),
                code="validation_error",
                fields={"sort_order": ["Must be between 0 and 32767."]},
            )

    @staticmethod
    def _assert_teacher_type_identity_free(
        *, name: str, type_slug: str, exclude_pk: int | None = None
    ) -> None:
        qs = TeacherType.objects.all()
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        errors = {}
        if qs.filter(name__iexact=name).exists():
            errors["name"] = ["Already used."]
        if qs.filter(slug__iexact=type_slug).exists():
            errors["slug"] = ["Already used."]
        if errors:
            raise ConflictException(
                _("A teacher type with this name or slug already exists."),
                code="teacher_type_exists",
                fields=errors,
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
