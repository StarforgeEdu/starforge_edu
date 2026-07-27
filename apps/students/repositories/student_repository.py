"""ORM-backed student repository — delegates scoping to the (preserved) selectors."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.students.interfaces.repositories import (
    IEnrollmentReasonRepository,
    IStudentRepository,
)
from apps.students.models import EnrollmentReason, StudentProfile
from core.repositories import BaseRepository


class EnrollmentReasonRepository(BaseRepository[EnrollmentReason], IEnrollmentReasonRepository):
    model = EnrollmentReason

    def list_reasons(self) -> QuerySet[EnrollmentReason]:
        return EnrollmentReason.objects.all()

    def get(self, *, pk: int) -> EnrollmentReason | None:
        return EnrollmentReason.objects.filter(pk=pk).first()

    def add(self, *, data: dict[str, Any]) -> EnrollmentReason:
        return EnrollmentReason.objects.create(**data)

    def apply_changes(self, reason: EnrollmentReason, *, changes: dict[str, Any]) -> EnrollmentReason:
        for field, value in changes.items():
            setattr(reason, field, value)
        if changes:
            reason.save(update_fields=[*changes.keys(), "updated_at"])
        return reason

    def remove(self, reason: EnrollmentReason) -> None:
        reason.delete()

    def slug_taken(self, *, slug: str, exclude_pk: int | None = None) -> bool:
        qs = EnrollmentReason.objects.filter(slug=slug)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()

    def active_slugs(self) -> set[str]:
        return set(EnrollmentReason.objects.filter(is_active=True).values_list("slug", flat=True))


class StudentRepository(BaseRepository[StudentProfile], IStudentRepository):
    model = StudentProfile

    def get_queryset(self) -> QuerySet[StudentProfile]:
        return StudentProfile.objects.select_related("user", "branch", "current_cohort")

    def scoped(self, *, user, roles) -> QuerySet[StudentProfile]:
        from apps.students.selectors import scoped_students  # role-based, select_related baked in

        return scoped_students(user=user, roles=roles)

    def get_scoped(self, *, user, roles, pk: int) -> StudentProfile | None:
        return self.scoped(user=user, roles=roles).filter(pk=pk).first()

    def profile_for(self, user) -> StudentProfile | None:
        from apps.students.selectors import student_profile_for

        return student_profile_for(user)
