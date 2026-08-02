"""Academics application services (staff CRUD + delegation to the preserved
grade/exam-result/transcript domain functions in apps.academics.services)."""

from __future__ import annotations

from datetime import date
from typing import Any, cast

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.db.models.deletion import ProtectedError
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.academics import services as domain
from apps.academics.integrity import ExamReadiness
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
from apps.academics.models import (
    Exam,
    ExamLifecycleEvent,
    ExamResult,
    ExamType,
    Grade,
    Subject,
    Transcript,
)
from apps.cohorts.models import Cohort
from apps.schedule.models import Term
from core.exceptions import ConflictException, ValidationException


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException(_("Invalid input."), code="validation_error", fields={field: [message]})


def _audit_catalog(*, instance, actor, action: str, before=None, after=None) -> None:
    from apps.audit.context import current_request
    from apps.audit.scopes import organization_audit_scope
    from apps.audit.services import audit_log_on_commit

    audit_log_on_commit(
        actor=actor,
        action=action,
        resource_type=f"academics.{instance.__class__.__name__}",
        resource_id=instance.pk,
        before=before,
        after=after,
        request=current_request(),
        scope=organization_audit_scope(),
    )


def _audit_exam(*, exam: Exam, actor, action: str, before=None, after=None) -> None:
    from apps.audit.context import current_request
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_log_on_commit

    audit_log_on_commit(
        actor=actor,
        action=action,
        resource_type="academics.Exam",
        resource_id=exam.pk,
        before=before,
        after=after,
        request=current_request(),
        scope=scoped_audit_scope(exam.cohort.branch_id, exam.cohort.department_id),
    )


class ExamTypeService(IExamTypeService):
    def __init__(self, repository: IExamTypeRepository) -> None:
        self.repository = repository

    def list_types(self) -> QuerySet[ExamType]:
        return self.repository.list_types()

    def get(self, *, pk: int) -> ExamType | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any], actor=None) -> ExamType:
        data = dict(data)
        if not data.get("slug"):
            # Auto-derive from the label so managers just type a name (LessonType parity).
            data["slug"] = slugify(data.get("name", ""))[:64]
        if not data["slug"]:
            raise _reject("slug", "Could not derive a slug; provide one explicitly.")
        data["slug"] = data["slug"].lower()
        if self.repository.name_taken(name=data["name"]):
            raise _reject("name", "An exam type with this name already exists.")
        if self.repository.slug_taken(slug=data["slug"]):
            raise _reject("slug", "An exam type with this slug already exists.")
        try:
            with transaction.atomic():
                exam_type = self.repository.add(data=data)
                from apps.audit.models import AuditLog
                from apps.audit.services import serialize_instance

                _audit_catalog(
                    instance=exam_type,
                    actor=actor,
                    action=AuditLog.Action.CREATE,
                    after=serialize_instance(exam_type),
                )
                return exam_type
        except IntegrityError:
            raise _reject("name", "An exam type with this name or slug already exists.") from None

    def update(self, exam_type: ExamType, *, changes: dict[str, Any], actor=None) -> ExamType:
        changes = dict(changes)
        if "slug" in changes:
            changes["slug"] = changes["slug"].lower()
        if "name" in changes and self.repository.name_taken(
            name=changes["name"],
            exclude_pk=exam_type.pk,
        ):
            raise _reject("name", "An exam type with this name already exists.")
        if "slug" in changes and self.repository.slug_taken(slug=changes["slug"], exclude_pk=exam_type.pk):
            raise _reject("slug", "An exam type with this slug already exists.")
        from apps.audit.models import AuditLog
        from apps.audit.services import diff_snapshots, serialize_instance

        before = serialize_instance(exam_type)
        try:
            with transaction.atomic():
                exam_type = self.repository.apply_changes(exam_type, changes=changes)
                _audit_catalog(
                    instance=exam_type,
                    actor=actor,
                    action=AuditLog.Action.UPDATE,
                    before=before,
                    after=diff_snapshots(before, serialize_instance(exam_type)),
                )
                return exam_type
        except IntegrityError:
            raise _reject("name", "An exam type with this name or slug already exists.") from None

    def delete(self, exam_type: ExamType, *, actor=None) -> None:
        from apps.audit.models import AuditLog
        from apps.audit.services import serialize_instance

        before = serialize_instance(exam_type)
        try:
            with transaction.atomic():
                _audit_catalog(
                    instance=exam_type,
                    actor=actor,
                    action=AuditLog.Action.DELETE,
                    before=before,
                )
                self.repository.remove(exam_type)
        except ProtectedError:
            raise ConflictException(
                _("This exam type is referenced by an exam and cannot be deleted."),
                code="exam_type_in_use",
            ) from None


class SubjectService(ISubjectService):
    def __init__(self, repository: ISubjectRepository) -> None:
        self.repository = repository

    def list_subjects(self) -> QuerySet[Subject]:
        return self.repository.list_subjects()

    def get(self, *, pk: int) -> Subject | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any], actor=None) -> Subject:
        self._validate_fks(data)
        if self.repository.name_taken(name=data["name"]):
            raise _reject("name", "A subject with this name already exists.")
        if self.repository.code_taken(code=data["code"]):
            raise _reject("code", "A subject with this code already exists.")
        try:
            with transaction.atomic():
                subject = self.repository.add(data=data)
                from apps.audit.models import AuditLog
                from apps.audit.services import serialize_instance

                _audit_catalog(
                    instance=subject,
                    actor=actor,
                    action=AuditLog.Action.CREATE,
                    after=serialize_instance(subject),
                )
                return subject
        except IntegrityError:
            raise _reject("name", "A subject with this name or code already exists.") from None

    def update(self, subject: Subject, *, changes: dict[str, Any], actor=None) -> Subject:
        self._validate_fks(changes)
        if "name" in changes and self.repository.name_taken(
            name=changes["name"],
            exclude_pk=subject.pk,
        ):
            raise _reject("name", "A subject with this name already exists.")
        if "code" in changes and self.repository.code_taken(code=changes["code"], exclude_pk=subject.pk):
            raise _reject("code", "A subject with this code already exists.")
        from apps.audit.models import AuditLog
        from apps.audit.services import diff_snapshots, serialize_instance

        before = serialize_instance(subject)
        try:
            with transaction.atomic():
                subject = self.repository.apply_changes(subject, changes=changes)
                _audit_catalog(
                    instance=subject,
                    actor=actor,
                    action=AuditLog.Action.UPDATE,
                    before=before,
                    after=diff_snapshots(before, serialize_instance(subject)),
                )
                return subject
        except IntegrityError:
            raise _reject("name", "A subject with this name or code already exists.") from None

    @staticmethod
    def _validate_fks(data: dict[str, Any]) -> None:
        dept_id = data.get("department_id")
        if dept_id is not None:
            from apps.org.models import Department

            if not Department.objects.filter(pk=dept_id).exists():
                raise _reject("department", "Department does not exist.")

    def delete(self, subject: Subject, *, actor=None) -> None:
        from apps.audit.models import AuditLog
        from apps.audit.services import serialize_instance

        before = serialize_instance(subject)
        try:
            with transaction.atomic():
                _audit_catalog(
                    instance=subject,
                    actor=actor,
                    action=AuditLog.Action.DELETE,
                    before=before,
                )
                self.repository.remove(subject)
        except ProtectedError:
            raise ConflictException(
                _("This subject is referenced and cannot be deleted."),
                code="subject_in_use",
            ) from None


class ExamService(IExamService):
    def __init__(self, repository: IExamRepository) -> None:
        self.repository = repository

    def scoped(
        self,
        *,
        user: Any,
        roles: set[str] | None,
        permission: str = "academics:read",
    ) -> QuerySet[Exam]:
        return self.repository.scoped(user=user, roles=roles, permission=permission)

    def get_scoped(
        self,
        *,
        pk: int,
        user: Any,
        roles: set[str] | None,
        permission: str = "academics:read",
    ) -> Exam | None:
        return self.repository.get_scoped(
            pk=pk,
            user=user,
            roles=roles,
            permission=permission,
        )

    def _resolve_write_fields(self, data: dict[str, Any], writable_cohort_ids) -> dict[str, Any]:
        """Resolve subject/term/cohort ids → *_id create kwargs, raising a clean 400
        on a missing row. A non-staff caller may only write into a cohort they teach
        (writable_cohort_ids is None for staff/superuser = the whole tenant)."""
        out: dict[str, Any] = {}
        if "subject" in data:
            if not Subject.objects.filter(pk=data["subject"], is_active=True).exists():
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
            elif not ExamType.objects.filter(pk=exam_type_id, is_active=True).exists():
                raise _reject("exam_type", "Exam type does not exist.")
            else:
                out["exam_type_id"] = exam_type_id
        for field in ("title", "exam_date", "max_score", "weight"):
            if field in data:
                out[field] = data[field]
        return out

    def create(self, *, data: dict[str, Any], writable_cohort_ids, created_by=None) -> Exam:
        resolved = self._resolve_write_fields(data, writable_cohort_ids)
        self._validate_semantics(resolved)
        resolved["created_by"] = created_by
        with transaction.atomic():
            exam = self.repository.add(data=resolved)
            exam = Exam.objects.select_related("cohort", "subject", "term", "exam_type").get(pk=exam.pk)
            from apps.audit.models import AuditLog
            from apps.audit.services import serialize_instance

            _audit_exam(
                exam=exam,
                actor=created_by,
                action=AuditLog.Action.CREATE,
                after=serialize_instance(exam),
            )
            return exam

    @transaction.atomic
    def update(
        self,
        exam: Exam,
        *,
        changes: dict[str, Any],
        writable_cohort_ids,
        actor=None,
    ) -> Exam:
        exam = (
            # ``exam_type`` is optional. Lock only the Exam row so PostgreSQL
            # does not attempt ``FOR UPDATE`` on the nullable outer-join side.
            Exam.objects.select_for_update(of=("self",))
            .select_related("cohort", "subject", "term", "exam_type")
            .get(pk=exam.pk)
        )
        if exam.is_published or exam.requires_republish:
            raise ConflictException(
                _("Published exams can only change through the correction workflow."),
                code="exam_locked",
            )
        resolved = self._resolve_write_fields(changes, writable_cohort_ids)
        self._validate_semantics(resolved, exam=exam)
        actual = {field: value for field, value in resolved.items() if getattr(exam, field) != value}
        if not actual:
            return exam
        if exam.results.exists() and {"subject_id", "cohort_id", "term_id"} & set(actual):
            raise ConflictException(
                _("An exam with recorded results cannot be moved."),
                code="exam_has_results",
            )
        if "max_score" in actual:
            highest = exam.results.order_by("-score").values_list("score", flat=True).first()
            if highest is not None and actual["max_score"] < highest:
                raise ConflictException(
                    _("Maximum score cannot be lower than an existing result."),
                    code="max_score_below_result",
                )
        from apps.audit.models import AuditLog
        from apps.audit.services import diff_snapshots, serialize_instance

        before = serialize_instance(exam)
        actual["version"] = exam.version + 1
        exam = self.repository.apply_changes(exam, changes=actual)
        _audit_exam(
            exam=exam,
            actor=actor,
            action=AuditLog.Action.UPDATE,
            before=before,
            after=diff_snapshots(before, serialize_instance(exam)),
        )
        return exam

    @transaction.atomic
    def delete(self, exam: Exam, *, actor=None) -> None:
        exam = Exam.objects.select_for_update().select_related("cohort").get(pk=exam.pk)
        if exam.is_published or exam.requires_republish or exam.lifecycle_events.exists():
            raise ConflictException(
                _("Published exam evidence cannot be deleted."),
                code="exam_locked",
            )
        if exam.results.exists():
            raise ConflictException(
                _("An exam with recorded results cannot be deleted."),
                code="exam_has_results",
            )
        from apps.audit.models import AuditLog
        from apps.audit.services import serialize_instance

        before = serialize_instance(exam)
        _audit_exam(
            exam=exam,
            actor=actor,
            action=AuditLog.Action.DELETE,
            before=before,
        )
        try:
            self.repository.remove(exam)
        except ProtectedError:
            raise ConflictException(
                _("This exam is referenced and cannot be deleted."),
                code="exam_in_use",
            ) from None

    def results_for(self, exam: Exam) -> QuerySet[ExamResult]:
        return self.repository.results_for(exam)

    def record_results(self, *, exam: Exam, rows: list[dict], actor) -> dict:
        return domain.record_results(exam=exam, rows=rows, actor=actor)

    def import_csv(self, *, exam: Exam, csv_file, actor) -> dict:
        return domain.bulk_grade_import(exam=exam, csv_file=csv_file, actor=actor)

    def publish(
        self,
        *,
        exam: Exam,
        actor,
        expected_version: int,
        confirmed: bool,
    ) -> tuple[Exam, ExamReadiness]:
        published, readiness = domain.publish_exam(
            exam=exam,
            actor=actor,
            expected_version=expected_version,
            confirmed=confirmed,
        )
        return published, readiness

    def readiness(self, *, exam: Exam) -> ExamReadiness:
        return domain.exam_readiness(exam=exam)

    def correct(
        self,
        *,
        exam: Exam,
        changes: dict[str, Any],
        rows: list[dict[str, Any]],
        reason: str,
        expected_version: int,
        writable_cohort_ids,
        actor,
    ) -> tuple[Exam, ExamLifecycleEvent]:
        resolved = self._resolve_write_fields(changes, writable_cohort_ids)
        self._validate_semantics(resolved, exam=exam)
        return domain.correct_exam(
            exam=exam,
            changes=resolved,
            rows=rows,
            reason=reason,
            expected_version=expected_version,
            actor=actor,
        )

    def history(self, *, exam: Exam) -> QuerySet[ExamLifecycleEvent]:
        return self.repository.history_for(exam)

    @staticmethod
    def _validate_semantics(resolved: dict[str, Any], *, exam: Exam | None = None) -> None:
        subject_id = resolved.get("subject_id", getattr(exam, "subject_id", None))
        cohort_id = resolved.get("cohort_id", getattr(exam, "cohort_id", None))
        term_id = resolved.get("term_id", getattr(exam, "term_id", None))
        exam_date = resolved.get("exam_date", getattr(exam, "exam_date", None))
        if not all(value is not None for value in (subject_id, cohort_id, term_id, exam_date)):
            return
        subject = Subject.objects.get(pk=cast(int, subject_id))
        cohort = Cohort.objects.select_related("department").get(pk=cast(int, cohort_id))
        term = Term.objects.get(pk=cast(int, term_id))
        target_date = cast(date, exam_date)
        if cohort.is_archived:
            raise _reject("cohort", "Archived cohorts cannot receive exams.")
        if subject.department_id is not None and subject.department_id != cohort.department_id:
            raise _reject("subject", "Subject and cohort must belong to the same department.")
        if target_date < term.start_date or target_date > term.end_date:
            raise _reject("exam_date", "Exam date must fall within the selected term.")


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
