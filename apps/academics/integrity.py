"""Assessment lifecycle, grade freshness, and database-write coordination.

All publication and correction transitions live here. Views/services resolve
authorization and foreign keys; this module owns state-machine and transactional
integrity so every transport has identical behavior.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.academics.dto import ResultFieldError, validate_result_values
from apps.academics.grading import display_for
from apps.academics.models import Exam, ExamLifecycleEvent, ExamResult, Grade
from apps.academics.signals import grade_changed
from apps.cohorts.models import CohortMembership
from apps.org.selectors import get_center_settings
from core.exceptions import ConflictException, UnprocessableEntity, ValidationException
from core.utils import current_schema

_HUNDRED = Decimal("100")
_GRADE_QUANTUM = Decimal("0.001")
_PROTECTED_EXAM_FIELDS = frozenset(
    {
        "subject_id",
        "cohort_id",
        "term_id",
        "exam_type_id",
        "title",
        "exam_date",
        "max_score",
        "weight",
    }
)
_MOVEMENT_FIELDS = frozenset({"subject_id", "cohort_id", "term_id"})


@dataclass(frozen=True, slots=True)
class ExamReadiness:
    exam_id: int
    version: int
    eligible: int
    graded: int
    missing: int
    excluded: int
    coverage_fraction: float
    ready: bool
    generated_at: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "exam": self.exam_id,
            "version": self.version,
            "eligible": self.eligible,
            "graded": self.graded,
            "missing": self.missing,
            "excluded": self.excluded,
            "sample_size": self.eligible,
            "coverage_fraction": self.coverage_fraction,
            "ready": self.ready,
            "generated_at": self.generated_at.isoformat(),
        }


@contextmanager
def assessment_integrity_write() -> Iterator[None]:
    """Temporarily authorize a reviewed service transition at the DB guard.

    The setting is transaction-local and restored even under ``ATOMIC_REQUESTS``
    or a nested call, so unrelated writes later in the request cannot borrow it.
    Non-PostgreSQL tooling receives the same atomic boundary without the setting.
    """
    with transaction.atomic():
        previous = "off"
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('starforge.academic_integrity_write', true)")
                previous = cursor.fetchone()[0] or "off"
                cursor.execute("SELECT set_config('starforge.academic_integrity_write', 'on', true)")
        try:
            yield
        finally:
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('starforge.academic_integrity_write', %s, true)",
                        [previous],
                    )


def exam_readiness(*, exam: Exam) -> ExamReadiness:
    """Return a single-query-set publication coverage snapshot."""
    eligible_ids = set(
        CohortMembership.objects.filter(cohort_id=exam.cohort_id, end_date__isnull=True)
        .order_by()
        .values_list("student_id", flat=True)
    )
    result_ids = set(
        ExamResult.objects.filter(exam_id=exam.pk).order_by().values_list("student_id", flat=True)
    )
    graded = len(eligible_ids & result_ids)
    eligible = len(eligible_ids)
    missing = eligible - graded
    excluded = len(result_ids - eligible_ids)
    coverage = round(graded / eligible, 6) if eligible else 0.0
    return ExamReadiness(
        exam_id=exam.pk,
        version=exam.version,
        eligible=eligible,
        graded=graded,
        missing=missing,
        excluded=excluded,
        coverage_fraction=coverage,
        ready=eligible > 0 and missing == 0 and excluded == 0,
        generated_at=timezone.now(),
    )


def _version_guard(exam: Exam, expected_version: int) -> None:
    if expected_version != exam.version:
        raise ConflictException(
            _("The exam changed after it was reviewed."),
            code="exam_version_conflict",
            fields={"expected_version": [f"Current version is {exam.version}."]},
        )


def _actor_repr(actor: Any) -> str:
    if actor is None or not getattr(actor, "is_authenticated", False):
        return ""
    return str(actor)[:255]


def _scope(exam: Exam) -> tuple[int, int | None]:
    return exam.cohort.branch_id, exam.cohort.department_id


def _event(
    *,
    exam: Exam,
    event_type: str,
    actor: Any,
    reason: str = "",
    details: dict[str, Any],
) -> ExamLifecycleEvent:
    branch_id, department_id = _scope(exam)
    return ExamLifecycleEvent.objects.create(
        exam=exam,
        event_type=event_type,
        exam_version=exam.version,
        reason=reason,
        details=details,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_repr=_actor_repr(actor),
        branch_id_snapshot=branch_id,
        department_id_snapshot=department_id,
    )


def _audit_exam(
    *,
    exam: Exam,
    actor: Any,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    from apps.audit.context import current_request
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_log_on_commit

    branch_id, department_id = _scope(exam)
    audit_log_on_commit(
        actor=actor,
        action=action,
        resource_type="academics.Exam",
        resource_id=exam.pk,
        before=before,
        after=after,
        request=current_request(),
        scope=scoped_audit_scope(branch_id, department_id),
    )


def _exam_snapshot(exam: Exam) -> dict[str, Any]:
    return {
        "subject_id": exam.subject_id,
        "cohort_id": exam.cohort_id,
        "term_id": exam.term_id,
        "exam_type_id": exam.exam_type_id,
        "title": exam.title,
        "exam_date": exam.exam_date.isoformat(),
        "max_score": str(exam.max_score),
        "weight": str(exam.weight),
        "is_published": exam.is_published,
        "published_at": exam.published_at.isoformat() if exam.published_at else None,
        "version": exam.version,
        "requires_republish": exam.requires_republish,
    }


def _result_snapshot(result: ExamResult) -> dict[str, Any]:
    return {
        "student": result.student_id,
        "student_code": result.student.student_id,
        "score": str(result.score),
        "note": result.note,
    }


def _audit_grade_rows(
    *,
    created: list[Grade],
    updated: list[tuple[Grade, dict[str, Any]]],
) -> None:
    from apps.audit.context import current_actor, current_request
    from apps.audit.models import AuditLog
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_logs_bulk_on_commit, diff_snapshots, serialize_instance
    from apps.students.models import StudentProfile

    actor = current_actor()
    request = current_request()
    all_grades = [*created, *(grade for grade, _before in updated)]
    scope_rows = StudentProfile.objects.filter(pk__in={grade.student_id for grade in all_grades}).values_list(
        "pk", "branch_id", "current_cohort__department_id"
    )
    scopes = {
        student_id: scoped_audit_scope(branch_id, department_id)
        for student_id, branch_id, department_id in scope_rows
    }
    entries = []
    for grade in created:
        scope = scopes.get(grade.student_id)
        if scope is None:
            continue
        entries.append(
            {
                "actor": actor,
                "action": AuditLog.Action.CREATE,
                "resource_type": "academics.Grade",
                "resource_id": grade.pk,
                "after": serialize_instance(grade),
                "request": request,
                "scope": scope,
            }
        )
    entries.extend(
        {
            "actor": actor,
            "action": AuditLog.Action.UPDATE,
            "resource_type": "academics.Grade",
            "resource_id": grade.pk,
            "before": before,
            "after": diff_snapshots(before, serialize_instance(grade)),
            "request": request,
            "scope": scopes[grade.student_id],
        }
        for grade, before in updated
        if grade.student_id in scopes
    )
    audit_logs_bulk_on_commit(entries)


@transaction.atomic
def recompute_term_grades(
    *,
    student_ids: Iterable[int],
    subject_id: int,
    term_id: int,
    publish: bool | None = None,
) -> list[Grade]:
    """Recompute a bounded student set with constant query growth.

    ``publish=None`` preserves each existing row's publication state. ``True``
    explicitly validates and publishes the recalculated rows. Missing academic
    evidence invalidates a stale row instead of leaving old components live.
    """
    ids = sorted(set(student_ids))
    if not ids:
        return []
    settings = get_center_settings()
    results = list(
        ExamResult.objects.filter(
            student_id__in=ids,
            exam__subject_id=subject_id,
            exam__term_id=term_id,
            exam__is_published=True,
            exam__requires_republish=False,
        )
        .select_related("exam")
        .order_by("student_id", "exam__exam_date", "exam_id")
    )
    grouped: dict[int, list[ExamResult]] = defaultdict(list)
    for result in results:
        grouped[result.student_id].append(result)

    from apps.audit.services import serialize_instance

    existing = {
        grade.student_id: grade
        for grade in Grade.objects.select_for_update().filter(
            student_id__in=ids,
            subject_id=subject_id,
            term_id=term_id,
        )
    }
    now = timezone.now()
    to_create: list[Grade] = []
    to_update: list[tuple[Grade, dict[str, Any]]] = []
    output: list[Grade] = []
    for student_id in ids:
        grade = existing.get(student_id)
        components, value_raw = _grade_components(grouped.get(student_id, []))
        if value_raw is None:
            if grade is not None and grade.is_valid:
                before = serialize_instance(grade)
                grade.is_valid = False
                grade.invalidated_at = now
                grade.invalidation_reason = "no_published_evidence"
                grade.is_published = False
                grade.published_at = None
                grade.computed_at = now
                to_update.append((grade, before))
            continue

        if grade is None:
            grade = Grade(
                student_id=student_id,
                subject_id=subject_id,
                term_id=term_id,
                value_raw=value_raw,
                value_display=display_for(value_raw, settings.grading_scheme),
                components=components,
                is_valid=True,
                is_published=publish is True,
                published_at=now if publish is True else None,
                computed_at=now,
            )
            to_create.append(grade)
        else:
            before = serialize_instance(grade)
            grade.value_raw = value_raw
            grade.value_display = display_for(value_raw, settings.grading_scheme)
            grade.components = components
            grade.is_valid = True
            grade.invalidated_at = None
            grade.invalidation_reason = ""
            grade.computed_at = now
            if publish is True:
                grade.is_published = True
                grade.published_at = now
            elif publish is False:
                grade.is_published = False
                grade.published_at = None
            to_update.append((grade, before))
        output.append(grade)

    if to_create:
        Grade.objects.bulk_create(to_create)
    if to_update:
        Grade.objects.bulk_update(
            [grade for grade, _before in to_update],
            fields=(
                "value_raw",
                "value_display",
                "components",
                "is_valid",
                "invalidated_at",
                "invalidation_reason",
                "is_published",
                "published_at",
                "computed_at",
            ),
        )
    _audit_grade_rows(created=to_create, updated=to_update)
    return output


def _grade_components(results: list[ExamResult]) -> tuple[list[dict[str, Any]], Decimal | None]:
    total_weight = Decimal("0")
    accumulator = Decimal("0")
    components: list[dict[str, Any]] = []
    for result in results:
        exam = result.exam
        fraction = result.score / exam.max_score
        accumulator += fraction * exam.weight
        total_weight += exam.weight
        components.append(
            {
                "exam": exam.pk,
                "title": exam.title,
                "score": str(result.score),
                "max_score": str(exam.max_score),
                "weight": str(exam.weight),
                "exam_version": exam.version,
            }
        )
    if total_weight == 0:
        return components, None
    raw = (_HUNDRED * accumulator / total_weight).quantize(
        _GRADE_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    return components, raw


@transaction.atomic
def invalidate_term_grades(
    *,
    student_ids: Iterable[int],
    subject_id: int,
    term_id: int,
    reason: str,
) -> int:
    ids = sorted(set(student_ids))
    if not ids:
        return 0
    from apps.audit.services import serialize_instance

    grades = list(
        Grade.objects.select_for_update().filter(
            student_id__in=ids,
            subject_id=subject_id,
            term_id=term_id,
        )
    )
    if not grades:
        return 0
    before = {grade.pk: serialize_instance(grade) for grade in grades}
    now = timezone.now()
    for grade in grades:
        grade.is_valid = False
        grade.invalidated_at = now
        grade.invalidation_reason = reason
        grade.is_published = False
        grade.published_at = None
        grade.computed_at = now
    Grade.objects.bulk_update(
        grades,
        fields=(
            "is_valid",
            "invalidated_at",
            "invalidation_reason",
            "is_published",
            "published_at",
            "computed_at",
        ),
    )
    _audit_grade_rows(created=[], updated=[(grade, before[grade.pk]) for grade in grades])
    return len(grades)


@transaction.atomic
def publish_exam(
    *,
    exam: Exam,
    actor: Any,
    expected_version: int,
    confirmed: bool,
) -> tuple[Exam, ExamReadiness]:
    if confirmed is not True:
        raise ValidationException(
            _("Publication requires an explicit confirmation."),
            code="publication_confirmation_required",
            fields={"confirmed": [str(_("Confirm the reviewed readiness snapshot."))]},
        )
    exam = (
        # Lock the assessment row, not every joined catalogue row. ``exam_type``
        # is nullable, so an unrestricted PostgreSQL ``FOR UPDATE`` across this
        # ``select_related`` graph fails on the nullable side of the outer join.
        Exam.objects.select_for_update(of=("self",))
        .select_related("cohort", "subject", "term", "exam_type")
        .get(pk=exam.pk)
    )
    _version_guard(exam, expected_version)
    readiness = exam_readiness(exam=exam)
    if not readiness.ready:
        raise ConflictException(
            _("The exam is not ready to publish."),
            code="exam_not_ready",
            fields={"readiness": [readiness.as_dict()]},
        )

    before = _exam_snapshot(exam)
    if not exam.is_published or exam.requires_republish:
        with assessment_integrity_write():
            exam.is_published = True
            exam.requires_republish = False
            exam.published_at = timezone.now()
            exam.save(
                update_fields=(
                    "is_published",
                    "requires_republish",
                    "published_at",
                    "updated_at",
                )
            )

        student_ids = list(
            ExamResult.objects.filter(exam=exam).order_by().values_list("student_id", flat=True)
        )
        recompute_term_grades(
            student_ids=student_ids,
            subject_id=exam.subject_id,
            term_id=exam.term_id,
            publish=True,
        )

    _event_row, created = ExamLifecycleEvent.objects.get_or_create(
        exam=exam,
        event_type=ExamLifecycleEvent.EventType.PUBLISHED,
        exam_version=exam.version,
        defaults={
            "details": {"readiness": readiness.as_dict()},
            "actor": actor if getattr(actor, "is_authenticated", False) else None,
            "actor_repr": _actor_repr(actor),
            "branch_id_snapshot": exam.cohort.branch_id,
            "department_id_snapshot": exam.cohort.department_id,
        },
    )
    if created:
        _audit_exam(
            exam=exam,
            actor=actor,
            action="update",
            before=before,
            after=_exam_snapshot(exam),
        )
        _notify_published_results(exam=exam, actor=actor)
    return exam, readiness


def _notify_published_results(*, exam: Exam, actor: Any) -> None:
    schema = current_schema()
    result_rows = list(ExamResult.objects.filter(exam=exam).select_related("student"))
    correction = (
        exam.lifecycle_events.filter(
            event_type=ExamLifecycleEvent.EventType.CORRECTED,
            exam_version=exam.version,
        )
        .only("details")
        .first()
    )
    old_by_student = {
        row["student"]: (row.get("before") or {}).get("score")
        for row in ((correction.details.get("result_changes") or []) if correction else [])
    }
    for result in result_rows:
        old_score = old_by_student.get(result.student_id)

        def notify(result: ExamResult = result, old_score: Any = old_score) -> None:
            grade_changed.send(
                sender=ExamResult,
                instance=result,
                old_score=Decimal(old_score) if old_score is not None else None,
                new_score=result.score,
                actor_id=getattr(actor, "pk", None),
                schema_name=schema,
            )

        transaction.on_commit(notify)


@transaction.atomic
def correct_exam(
    *,
    exam: Exam,
    changes: dict[str, Any],
    rows: list[dict[str, Any]],
    reason: str,
    expected_version: int,
    actor: Any,
) -> tuple[Exam, ExamLifecycleEvent]:
    """Withdraw, version, and audit one published-exam correction."""
    exam = (
        # See ``publish_exam``: the optional exam type is an outer join and must
        # not be included in the row-lock target set.
        Exam.objects.select_for_update(of=("self",))
        .select_related("cohort", "subject", "term", "exam_type")
        .get(pk=exam.pk)
    )
    _version_guard(exam, expected_version)
    if exam.requires_republish:
        raise ConflictException(
            _("This exam already has a correction awaiting republication."),
            code="exam_republish_required",
        )
    if not exam.is_published:
        raise ConflictException(
            _("Only a published exam uses the correction workflow."),
            code="exam_not_published",
        )

    unknown = set(changes) - _PROTECTED_EXAM_FIELDS
    if unknown:
        raise ValidationException(
            _("Invalid correction fields."),
            code="validation_error",
            fields={field: [str(_("This field cannot be corrected."))] for field in sorted(unknown)},
        )
    existing_results = list(
        ExamResult.objects.select_for_update()
        .filter(exam=exam)
        .select_related("student")
        .order_by("student_id")
    )
    actual_changes = {field: value for field, value in changes.items() if getattr(exam, field) != value}
    if _MOVEMENT_FIELDS & set(actual_changes) and existing_results:
        raise ConflictException(
            _("An exam with recorded results cannot be moved."),
            code="exam_has_results",
        )
    corrected_max = actual_changes.get("max_score", exam.max_score)
    highest_score = max((result.score for result in existing_results), default=Decimal("0"))
    if corrected_max < highest_score:
        raise ConflictException(
            _("Maximum score cannot be lower than an existing result."),
            code="max_score_below_result",
            fields={"max_score": [f"Highest recorded score is {highest_score}."]},
        )

    target_cohort_id = actual_changes.get("cohort_id", exam.cohort_id)
    normalized_rows = _normalize_correction_rows(
        rows=rows,
        max_score=corrected_max,
        cohort_id=target_cohort_id,
    )
    existing_by_student = {result.student_id: result for result in existing_results}
    result_changes: list[dict[str, Any]] = []
    before_by_student: dict[int, dict[str, Any]] = {}
    to_create: list[ExamResult] = []
    to_update: list[ExamResult] = []
    now = timezone.now()
    for row in normalized_rows:
        student = row["student"]
        result = existing_by_student.get(student.pk)
        before_result = _result_snapshot(result) if result is not None else None
        if result is None:
            result = ExamResult(
                exam=exam,
                student=student,
                score=row["score"],
                note=row["note"],
                graded_by=actor,
                graded_at=now,
            )
            to_create.append(result)
        elif result.score != row["score"] or result.note != row["note"]:
            if before_result is None:  # defensive invariant; keeps -O behavior identical
                raise RuntimeError("Existing assessment result has no correction snapshot")
            before_by_student[result.student_id] = before_result
            result.score = row["score"]
            result.note = row["note"]
            result.graded_by = actor
            result.graded_at = now
            to_update.append(result)
        else:
            continue
        result_changes.append(
            {
                "student": student.pk,
                "student_code": student.student_id,
                "before": before_result,
                "after": {
                    "student": student.pk,
                    "student_code": student.student_id,
                    "score": str(result.score),
                    "note": result.note,
                },
            }
        )

    if not actual_changes and not result_changes:
        raise ValidationException(
            _("The correction does not change the exam or any result."),
            code="correction_no_changes",
        )

    before_exam = _exam_snapshot(exam)
    old_subject_id = exam.subject_id
    old_term_id = exam.term_id
    affected_student_ids = {result.student_id for result in existing_results}
    affected_student_ids.update(row["student"].pk for row in normalized_rows)
    with assessment_integrity_write():
        for field, value in actual_changes.items():
            setattr(exam, field, value)
        exam.version += 1
        exam.is_published = False
        exam.requires_republish = True
        exam.published_at = None
        exam.save(
            update_fields=(
                *actual_changes.keys(),
                "version",
                "is_published",
                "requires_republish",
                "published_at",
                "updated_at",
            )
        )
        if to_create:
            ExamResult.objects.bulk_create(to_create)
        if to_update:
            ExamResult.objects.bulk_update(
                to_update,
                fields=("score", "note", "graded_by", "graded_at"),
            )

    invalidate_term_grades(
        student_ids=affected_student_ids,
        subject_id=old_subject_id,
        term_id=old_term_id,
        reason="exam_correction_pending",
    )
    if (exam.subject_id, exam.term_id) != (old_subject_id, old_term_id):
        invalidate_term_grades(
            student_ids=affected_student_ids,
            subject_id=exam.subject_id,
            term_id=exam.term_id,
            reason="exam_correction_pending",
        )

    details = {
        "exam_changes": {
            field: {
                "before": _json_value(before_exam[field]),
                "after": _json_value(value),
            }
            for field, value in actual_changes.items()
        },
        "result_changes": result_changes,
        "publication_withdrawn": True,
    }
    event = _event(
        exam=exam,
        event_type=ExamLifecycleEvent.EventType.CORRECTED,
        actor=actor,
        reason=reason,
        details=details,
    )
    _audit_exam(
        exam=exam,
        actor=actor,
        action="update",
        before=before_exam,
        after=_exam_snapshot(exam),
    )
    _audit_corrected_results(
        exam=exam,
        actor=actor,
        created=to_create,
        updated=to_update,
        before_by_student=before_by_student,
    )
    return exam, event


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _normalize_correction_rows(
    *,
    rows: list[dict[str, Any]],
    max_score: Decimal,
    cohort_id: int,
) -> list[dict[str, Any]]:
    if len(rows) > 5000:
        raise ValidationException(
            _("Too many correction rows."),
            code="too_many_rows",
            fields={"results": [str(_("At most 5000 rows are allowed."))]},
        )
    seen: set[int] = set()
    errors: dict[str, list[str]] = {}
    normalized: list[dict[str, Any]] = []
    member_ids = set(
        CohortMembership.objects.filter(cohort_id=cohort_id, end_date__isnull=True)
        .order_by()
        .values_list("student_id", flat=True)
    )
    for index, row in enumerate(rows):
        student = row["student"]
        if student.pk in seen:
            errors[str(index)] = [str(_("A student may appear only once."))]
            continue
        seen.add(student.pk)
        if student.pk not in member_ids:
            errors[str(index)] = [str(_("Student is not enrolled in the exam cohort."))]
            continue
        try:
            values = validate_result_values(
                score=row.get("score"),
                note=row.get("note", ""),
                max_score=max_score,
            )
        except ResultFieldError as exc:
            errors[str(index)] = [exc.message]
            continue
        normalized.append({"student": student, "score": values.score, "note": values.note})
    if errors:
        raise UnprocessableEntity(
            _("One or more correction rows are invalid."),
            code="result_validation_failed",
            fields=errors,
        )
    return normalized


def _audit_corrected_results(
    *,
    exam: Exam,
    actor: Any,
    created: list[ExamResult],
    updated: list[ExamResult],
    before_by_student: dict[int, dict[str, Any]],
) -> None:
    from apps.audit.context import current_request
    from apps.audit.models import AuditLog
    from apps.audit.scopes import scoped_audit_scope
    from apps.audit.services import audit_logs_bulk_on_commit, diff_snapshots, serialize_instance

    scope = scoped_audit_scope(exam.cohort.branch_id, exam.cohort.department_id)
    request = current_request()
    entries = [
        {
            "actor": actor,
            "action": AuditLog.Action.CREATE,
            "resource_type": "academics.ExamResult",
            "resource_id": result.pk,
            "after": serialize_instance(result),
            "request": request,
            "scope": scope,
        }
        for result in created
    ]
    entries.extend(
        {
            "actor": actor,
            "action": AuditLog.Action.UPDATE,
            "resource_type": "academics.ExamResult",
            "resource_id": result.pk,
            "before": before_by_student.get(result.student_id),
            "after": diff_snapshots(
                before_by_student.get(result.student_id),
                serialize_instance(result),
            ),
            "request": request,
            "scope": scope,
        }
        for result in updated
    )
    audit_logs_bulk_on_commit(entries)
