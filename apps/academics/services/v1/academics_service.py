"""Academics application services (staff CRUD + delegation to the preserved
grade/exam-result/transcript domain functions in apps.academics.services)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.text import slugify

from apps.academics import services as domain
from apps.academics.interfaces.repositories import (
    IExamRepository,
    IExamTypeRepository,
    IGradeRepository,
    ISubjectRepository,
    ITranscriptRepository,
)
from apps.academics.interfaces.services import (
    IExamService,
    IExamTypeService,
    IGradeService,
    ISubjectService,
    ITranscriptService,
)
from apps.academics.models import Exam, ExamResult, ExamType, Grade, Subject, Transcript
from apps.cohorts.models import Cohort
from apps.schedule.models import Term
from core.exceptions import ValidationException


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


class ExamTypeService(IExamTypeService):
    def __init__(self, repository: IExamTypeRepository) -> None:
        self.repository = repository

    def list_types(self) -> QuerySet[ExamType]:
        return self.repository.list_types()

    def get(self, *, pk: int) -> ExamType | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any]) -> ExamType:
        data = dict(data)
        if not data.get("slug"):
            # Auto-derive from the label so managers just type a name (LessonType parity).
            data["slug"] = slugify(data.get("name", ""))[:64]
        if not data["slug"]:
            raise _reject("slug", "Could not derive a slug; provide one explicitly.")
        if self.repository.slug_taken(slug=data["slug"]):
            raise _reject("slug", "An exam type with this slug already exists.")
        return self.repository.add(data=data)

    def update(self, exam_type: ExamType, *, changes: dict[str, Any]) -> ExamType:
        if "slug" in changes and self.repository.slug_taken(slug=changes["slug"], exclude_pk=exam_type.pk):
            raise _reject("slug", "An exam type with this slug already exists.")
        return self.repository.apply_changes(exam_type, changes=changes)

    def delete(self, exam_type: ExamType) -> None:
        self.repository.remove(exam_type)


class SubjectService(ISubjectService):
    def __init__(self, repository: ISubjectRepository) -> None:
        self.repository = repository

    def list_subjects(self) -> QuerySet[Subject]:
        return self.repository.list_subjects()

    def get(self, *, pk: int) -> Subject | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any]) -> Subject:
        self._validate_fks(data)
        if self.repository.code_taken(code=data["code"]):
            raise _reject("code", "A subject with this code already exists.")
        return self.repository.add(data=data)

    def update(self, subject: Subject, *, changes: dict[str, Any]) -> Subject:
        self._validate_fks(changes)
        if "code" in changes and self.repository.code_taken(code=changes["code"], exclude_pk=subject.pk):
            raise _reject("code", "A subject with this code already exists.")
        return self.repository.apply_changes(subject, changes=changes)

    @staticmethod
    def _validate_fks(data: dict[str, Any]) -> None:
        dept_id = data.get("department_id")
        if dept_id is not None:
            from apps.org.models import Department

            if not Department.objects.filter(pk=dept_id).exists():
                raise _reject("department", "Department does not exist.")

    def delete(self, subject: Subject) -> None:
        self.repository.remove(subject)


class ExamService(IExamService):
    def __init__(self, repository: IExamRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Exam]:
        return self.repository.scoped(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> Exam | None:
        return self.repository.get_scoped(pk=pk, user=user, roles=roles)

    def _resolve_write_fields(self, data: dict[str, Any], writable_cohort_ids) -> dict[str, Any]:
        """Resolve subject/term/cohort ids → *_id create kwargs, raising a clean 400
        on a missing row. A non-staff caller may only write into a cohort they teach
        (writable_cohort_ids is None for staff/superuser = the whole tenant)."""
        out: dict[str, Any] = {}
        if "subject" in data:
            if not Subject.objects.filter(pk=data["subject"]).exists():
                raise _reject("subject", "Subject does not exist.")
            out["subject_id"] = data["subject"]
        if "term" in data:
            if not Term.objects.filter(pk=data["term"]).exists():
                raise _reject("term", "Term does not exist.")
            out["term_id"] = data["term"]
        if "cohort" in data:
            cohort_id = data["cohort"]
            if writable_cohort_ids is not None and cohort_id not in writable_cohort_ids:
                # Mirror the old serializer's scoped cohort queryset → out-of-scope 400.
                raise _reject("cohort", "This cohort is not in your writable cohorts.")
            if not Cohort.objects.filter(pk=cohort_id).exists():
                raise _reject("cohort", "Cohort does not exist.")
            out["cohort_id"] = cohort_id
        if "exam_type" in data:
            exam_type_id = data["exam_type"]
            if exam_type_id is None:
                out["exam_type_id"] = None  # nullable — clearing the type
            elif not ExamType.objects.filter(pk=exam_type_id).exists():
                raise _reject("exam_type", "Exam type does not exist.")
            else:
                out["exam_type_id"] = exam_type_id
        for field in ("title", "exam_date", "max_score", "weight"):
            if field in data:
                out[field] = data[field]
        return out

    def create(self, *, data: dict[str, Any], writable_cohort_ids) -> Exam:
        return self.repository.add(data=self._resolve_write_fields(data, writable_cohort_ids))

    def update(self, exam: Exam, *, changes: dict[str, Any], writable_cohort_ids) -> Exam:
        return self.repository.apply_changes(
            exam, changes=self._resolve_write_fields(changes, writable_cohort_ids)
        )

    def delete(self, exam: Exam) -> None:
        self.repository.remove(exam)

    def results_for(self, exam: Exam) -> QuerySet[ExamResult]:
        return self.repository.results_for(exam)

    def record_results(self, *, exam: Exam, rows: list[dict], actor) -> dict:
        return domain.record_results(exam=exam, rows=rows, actor=actor)

    def import_csv(self, *, exam: Exam, csv_file, actor) -> dict:
        return domain.bulk_grade_import(exam=exam, csv_file=csv_file, actor=actor)

    def publish(self, *, exam: Exam, actor) -> Exam:
        return domain.publish_exam(exam=exam, actor=actor)


class GradeService(IGradeService):
    def __init__(self, repository: IGradeRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Grade]:
        return self.repository.scoped(user=user, roles=roles)

    def recompute(self, *, cohort, subject, term, publish: bool) -> list[Grade]:
        return domain.recompute_cohort_term(cohort=cohort, subject=subject, term=term, publish=publish)

    def honor_roll(self, *, term_id: int, user, roles: set[str] | None) -> QuerySet[Grade]:
        from apps.academics import selectors

        return selectors.honor_roll(term_id=term_id, user=user, roles=roles)

    def warnings(self, *, term_id: int, user, roles: set[str] | None) -> QuerySet[Grade]:
        from apps.academics import selectors

        return selectors.academic_warnings(term_id=term_id, user=user, roles=roles)


class TranscriptService(ITranscriptService):
    def __init__(self, repository: ITranscriptRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Transcript]:
        return self.repository.scoped(user=user, roles=roles)

    def request(self, *, student, term, requested_by) -> Transcript:
        return domain.request_transcript(student=student, term=term, requested_by=requested_by)
