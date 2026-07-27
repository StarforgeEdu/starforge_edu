"""Academics HTTP views (layered, off DRF).

Subjects (CRUD), exams (CRUD + per-student results record/list/CSV-import/publish,
cohort-scoped so a teacher only reaches their own cohorts), read-only computed
grades, grade recompute, transcripts (async PDF), and the staff-only honor-roll /
academic-warning aggregates. Raw per-student results are gated at ``academics:write``
even on read, so a student/parent holding ``academics:read`` can't harvest scores.
"""

from __future__ import annotations

import json
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt

from apps.academics.interfaces.services import (
    IExamService,
    IExamTypeService,
    IGradeService,
    ISubjectService,
    ITranscriptService,
)
from apps.academics.models import Exam
from apps.academics.presenters import (
    exam_result_to_dict,
    exam_to_dict,
    exam_type_to_dict,
    grade_to_dict,
    subject_to_dict,
    transcript_to_dict,
)
from apps.cohorts.models import Cohort
from apps.parents.models import Guardian
from apps.schedule.models import Term
from apps.students.models import StudentProfile
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import bool_field, decimal_field, parse_bool, read_json
from core.listing import apply_filters, paginate
from core.permissions import Role, get_user_roles, has_permission_code
from core.ratelimit import check_rate
from core.responses import created, error, no_content, paginated, success
from core.utils import current_schema

# Honor-roll / warnings are staff-facing aggregates (never exposed to the
# students/parents who also hold `academics:read`).
_REPORT_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT, Role.TEACHER}


# --- service accessors -----------------------------------------------------


def _subject_service() -> ISubjectService:
    return container.resolve(ISubjectService)  # type: ignore[type-abstract]


def _exam_service() -> IExamService:
    return container.resolve(IExamService)  # type: ignore[type-abstract]


def _exam_type_service() -> IExamTypeService:
    return container.resolve(IExamTypeService)  # type: ignore[type-abstract]


def _grade_service() -> IGradeService:
    return container.resolve(IGradeService)  # type: ignore[type-abstract]


def _transcript_service() -> ITranscriptService:
    return container.resolve(ITranscriptService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


# --- value validators (never-500 on bad input) -----------------------------


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require(data: dict[str, Any], name: str) -> Any:
    if name not in data or data[name] is None:
        raise _reject(name, "This field is required.")
    return data[name]


def _str_value(raw: Any, name: str, *, max_length: int | None = None, allow_blank: bool = False) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    value = raw.strip()
    if "\x00" in value:
        raise _reject(name, "Null characters are not allowed.")
    if not value and not allow_blank:
        raise _reject(name, "This field may not be blank.")
    if max_length is not None and len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _int_value(raw: Any, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise _reject(name, "A valid integer is required.")
    try:
        return int(str(raw).strip())
    except ValueError:
        raise _reject(name, "A valid integer is required.") from None


def _slug_value(raw: Any, name: str, *, max_length: int) -> str:
    import re

    value = _str_value(raw, name, max_length=max_length)
    if not re.fullmatch(r"[-a-zA-Z0-9_]+", value):
        raise _reject(name, "Enter a valid slug (letters, numbers, hyphens, underscores).")
    return value


def _choice_value(raw: Any, name: str, choices) -> str:
    if not isinstance(raw, str) or raw not in choices:
        raise _reject(name, f"Must be one of: {', '.join(choices)}.")
    return raw


def _date_value(raw: Any, name: str):
    if not isinstance(raw, str):
        raise _reject(name, "Enter a valid date (YYYY-MM-DD).")
    try:
        parsed = parse_date(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise _reject(name, "Enter a valid date (YYYY-MM-DD).")
    return parsed


def _require_int_qparam(request: HttpRequest, name: str) -> int:
    raw = request.GET.get(name)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValidationException(
            f"Query parameter '{name}' is required and must be an integer.",
            code="invalid_query_param",
            fields={name: ["This query parameter is required."]},
        ) from exc


# --- subjects --------------------------------------------------------------


def _subject_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    out: dict[str, Any] = {
        "name": _str_value(_require(data, "name"), "name", max_length=200),
        "code": _slug_value(_require(data, "code"), "code", max_length=50),
        "description": _str_value(data.get("description", ""), "description", allow_blank=True),
        "is_active": bool_field(data, "is_active", default=True),
    }
    if data.get("department") is not None:
        out["department_id"] = _int_value(data["department"], "department")
    return out


def _subject_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "name" in data:
        changes["name"] = _str_value(data["name"], "name", max_length=200)
    if "code" in data:
        changes["code"] = _slug_value(data["code"], "code", max_length=50)
    if "description" in data:
        changes["description"] = _str_value(data["description"], "description", allow_blank=True)
    if "is_active" in data:
        changes["is_active"] = parse_bool(data["is_active"], "is_active")
    if "department" in data:
        changes["department_id"] = (
            None if data["department"] is None else _int_value(data["department"], "department")
        )
    return changes


@csrf_exempt
@require_auth
def subjects_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "academics:read")
        qs = apply_filters(
            request,
            _subject_service().list_subjects(),
            filter_fields=("is_active", "department"),
            search_fields=("name", "code"),
            ordering_fields=("name", "code"),
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([subject_to_dict(s) for s in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "academics:write")
        subject = _subject_service().create(data=_subject_create_data(request))
        return created(subject_to_dict(subject))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def subject_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "academics:read" if read else "academics:write")
    subject = _subject_service().get(pk=pk)
    if subject is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(subject_to_dict(subject))
    if request.method in ("PUT", "PATCH"):
        return success(subject_to_dict(_subject_service().update(subject, changes=_subject_changes(request))))
    if request.method == "DELETE":
        _subject_service().delete(subject)
        return no_content()
    return _method_not_allowed()


# --- exam types (per-Center configurable exam kinds) -----------------------


def _exam_type_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    out: dict[str, Any] = {
        "name": _str_value(_require(data, "name"), "name", max_length=64),
        "color": _str_value(data.get("color", ""), "color", max_length=16, allow_blank=True),
        "is_active": bool_field(data, "is_active", default=True),
    }
    if data.get("slug"):
        out["slug"] = _slug_value(data["slug"], "slug", max_length=64)
    return out


def _exam_type_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "name" in data:
        changes["name"] = _str_value(data["name"], "name", max_length=64)
    if "slug" in data:
        changes["slug"] = _slug_value(data["slug"], "slug", max_length=64)
    if "color" in data:
        changes["color"] = _str_value(data["color"], "color", max_length=16, allow_blank=True)
    if "is_active" in data:
        changes["is_active"] = parse_bool(data["is_active"], "is_active")
    return changes


@csrf_exempt
@require_auth
def exam_types_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "academics:read")
        qs = apply_filters(
            request,
            _exam_type_service().list_types(),
            filter_fields=("is_active",),
            search_fields=("name", "slug"),
            ordering_fields=("name",),
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([exam_type_to_dict(t) for t in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "academics:write")
        exam_type = _exam_type_service().create(data=_exam_type_create_data(request))
        return created(exam_type_to_dict(exam_type))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def exam_type_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "academics:read" if read else "academics:write")
    exam_type = _exam_type_service().get(pk=pk)
    if exam_type is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(exam_type_to_dict(exam_type))
    if request.method in ("PUT", "PATCH"):
        return success(
            exam_type_to_dict(_exam_type_service().update(exam_type, changes=_exam_type_changes(request)))
        )
    if request.method == "DELETE":
        _exam_type_service().delete(exam_type)
        return no_content()
    return _method_not_allowed()


# --- exams -----------------------------------------------------------------


def _writable_cohort_ids(request: HttpRequest):
    """Cohorts the caller may write an exam into. None = unscoped (staff/superuser
    reach the whole tenant); a set = a TEACHER limited to cohorts they teach."""
    from apps.academics.selectors import STAFF_ROLES, _cohorts_taught_by

    user = request.user
    if getattr(user, "is_superuser", False):
        return None
    roles = get_user_roles(request)
    if roles & STAFF_ROLES:
        return None
    if Role.TEACHER in roles:
        return set(_cohorts_taught_by(user))
    return set()


def _exam_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    out: dict[str, Any] = {
        "subject": _int_value(_require(data, "subject"), "subject"),
        "cohort": _int_value(_require(data, "cohort"), "cohort"),
        "term": _int_value(_require(data, "term"), "term"),
        "exam_type": _int_value(_require(data, "exam_type"), "exam_type"),
        "title": _str_value(_require(data, "title"), "title", max_length=200),
        "exam_date": _date_value(_require(data, "exam_date"), "exam_date"),
    }
    _add_optional_decimals(data, out)
    return out


def _exam_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "subject" in data:
        changes["subject"] = _int_value(data["subject"], "subject")
    if "cohort" in data:
        changes["cohort"] = _int_value(data["cohort"], "cohort")
    if "term" in data:
        changes["term"] = _int_value(data["term"], "term")
    if "exam_type" in data:
        changes["exam_type"] = None if data["exam_type"] is None else _int_value(data["exam_type"], "exam_type")
    if "title" in data:
        changes["title"] = _str_value(data["title"], "title", max_length=200)
    if "exam_date" in data:
        changes["exam_date"] = _date_value(data["exam_date"], "exam_date")
    _add_optional_decimals(data, changes)
    return changes


def _add_optional_decimals(data: dict[str, Any], out: dict[str, Any]) -> None:
    """max_score (6,2) / weight (4,3) are optional (model defaults 100/1). Present
    with a value → validate; explicitly null/blank on a NOT-NULL column → 400."""
    for field, digits, places in (("max_score", 6, 2), ("weight", 4, 3)):
        if field not in data:
            continue
        value = decimal_field(data, field, max_digits=digits, decimal_places=places)
        if value is None:
            raise _reject(field, "This field may not be null.")
        out[field] = value


@csrf_exempt
@require_auth
def exams_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "academics:read")
        qs = _exam_service().scoped(user=request.user, roles=get_user_roles(request))
        qs = apply_filters(
            request,
            qs,
            filter_fields=("subject", "cohort", "term", "exam_type", "is_published"),
            ordering_fields=("exam_date",),
            default_ordering="-exam_date",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([exam_to_dict(e) for e in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "academics:write")
        exam = _exam_service().create(
            data=_exam_create_data(request), writable_cohort_ids=_writable_cohort_ids(request)
        )
        return created(exam_to_dict(exam))
    return _method_not_allowed()


def _get_exam_in_scope(request: HttpRequest, pk: int) -> Exam:
    exam = _exam_service().get_scoped(pk=pk, user=request.user, roles=get_user_roles(request))
    if exam is None:
        raise NotFoundException(code="not_found")
    return exam


@csrf_exempt
@require_auth
def exam_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "academics:read" if read else "academics:write")
    exam = _get_exam_in_scope(request, pk)
    if read:
        return success(exam_to_dict(exam))
    if request.method in ("PUT", "PATCH"):
        exam = _exam_service().update(
            exam, changes=_exam_changes(request), writable_cohort_ids=_writable_cohort_ids(request)
        )
        return success(exam_to_dict(exam))
    if request.method == "DELETE":
        _exam_service().delete(exam)
        return no_content()
    return _method_not_allowed()


def _parse_result_rows(request: HttpRequest) -> list[dict]:
    """The results payload is a top-level JSON ARRAY of {student, score, note?} —
    parse it directly (read_json rejects non-objects), validate each element, and
    resolve student ids to StudentProfile objects (record_results expects objects).
    An empty/malformed body is a 400 (parity with the old DRF ParseError); an explicit
    empty array [] is a valid no-op."""
    try:
        raw = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        raise _reject("rows", "Request body must be a JSON array.") from None
    if not isinstance(raw, list):
        raise _reject("rows", "Request body must be a JSON array.")
    # Bound the batch (parity with the CSV import's MAX_IMPORT_ROWS): each row does a
    # StudentProfile lookup here + per-row existing/upsert queries inside record_results'
    # single transaction, so an uncapped array (a ~2.5MB body is ~80k rows) means ~240k
    # queries in one long-held atomic — a DB/connection-pool hazard the CSV twin caps.
    from apps.academics.services import MAX_IMPORT_ROWS

    if len(raw) > MAX_IMPORT_ROWS:
        raise _reject("rows", f"Too many rows (max {MAX_IMPORT_ROWS}).")
    rows: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _reject(f"rows[{index}]", "Each row must be an object.")
        student_id = _int_value(_require(item, "student"), f"rows[{index}].student")
        score = decimal_field(item, "score", max_digits=6, decimal_places=2)
        if score is None:
            raise _reject(f"rows[{index}].score", "This field is required.")
        note = item.get("note", "")
        note = (
            ""
            if note in (None, "")
            else _str_value(note, f"rows[{index}].note", max_length=255, allow_blank=True)
        )
        student = StudentProfile.objects.filter(pk=student_id).first()
        if student is None:
            raise _reject(f"rows[{index}].student", "Student does not exist.")
        rows.append({"student": student, "score": score, "note": note})
    return rows


@csrf_exempt
@require_auth
def exam_results_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD", "POST"):
        return _method_not_allowed()
    # Raw per-student results are staff/teacher-only on READ and write.
    check_perm(request, "academics:write")
    exam = _get_exam_in_scope(request, pk)
    if request.method in ("GET", "HEAD"):
        return success([exam_result_to_dict(r) for r in _exam_service().results_for(exam)])
    rows = _parse_result_rows(request)
    result = _exam_service().record_results(exam=exam, rows=rows, actor=request.user)
    return success(
        {
            "created": result["created"],
            "updated": result["updated"],
            "results": [exam_result_to_dict(r) for r in result["results"]],
        }
    )


@csrf_exempt
@require_auth
def exam_import_csv_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "academics:write")
    # bulk_import throttle: 6/min per (schema, user) — mirrors BulkImportThrottle.
    check_rate(scope="bulk_import", key=f"{current_schema()}:{request.user.pk}", limit=6, window=60)
    exam = _get_exam_in_scope(request, pk)
    upload = request.FILES.get("file")
    if upload is None:
        raise _reject("file", "This field is required.")
    result = _exam_service().import_csv(exam=exam, csv_file=upload, actor=request.user)
    return success({"created": result["created"], "updated": result["updated"]})


@csrf_exempt
@require_auth
def exam_publish_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "academics:write")
    exam = _get_exam_in_scope(request, pk)
    exam = _exam_service().publish(exam=exam, actor=request.user)
    return success(exam_to_dict(exam))


# --- grades (read-only computed) -------------------------------------------


@csrf_exempt
@require_auth
def grades_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:read")
    qs = _grade_service().scoped(user=request.user, roles=get_user_roles(request))
    qs = apply_filters(
        request,
        qs,
        filter_fields=("student", "subject", "term", "is_published"),
        ordering_fields=("computed_at", "value_raw"),
        default_ordering="-computed_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([grade_to_dict(g) for g in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def grade_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:read")
    grade = _grade_service().scoped(user=request.user, roles=get_user_roles(request)).filter(pk=pk).first()
    if grade is None:
        raise NotFoundException(code="not_found")
    return success(grade_to_dict(grade))


@csrf_exempt
@require_auth
def grade_recompute_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "academics:write")
    data = read_json(request)
    cohort_id = _int_value(_require(data, "cohort"), "cohort")
    subject_id = _int_value(_require(data, "subject"), "subject")
    term_id = _int_value(_require(data, "term"), "term")
    publish = bool_field(data, "publish", default=False)
    from apps.academics.models import Subject

    cohort = Cohort.objects.filter(pk=cohort_id).first()
    subject = Subject.objects.filter(pk=subject_id).first()
    term = Term.objects.filter(pk=term_id).first()
    if cohort is None or subject is None or term is None:
        raise NotFoundException(code="not_found")
    # Scope the write like every other academics write path (exam create/update via
    # _resolve_write_fields): a TEACHER may only recompute/publish grades for cohorts
    # they teach. Without this a plain academics:write holder could force-publish another
    # cohort's (or another branch's) grades. None = staff/superuser, unscoped.
    writable = _writable_cohort_ids(request)
    if writable is not None and cohort_id not in writable:
        raise PermissionException(
            "You may only recompute grades for cohorts you teach.", code="forbidden"
        )
    grades = _grade_service().recompute(cohort=cohort, subject=subject, term=term, publish=publish)
    return success({"recomputed": len(grades)})


# --- transcripts -----------------------------------------------------------


def _is_self_or_child(request: HttpRequest, student) -> bool:
    user: Any = request.user  # @require_auth guarantees an authenticated User
    if student.user_id == user.id:
        return True
    return Guardian.objects.filter(student=student, parent__user=user).exists()


@csrf_exempt
@require_auth
def transcripts_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "academics:read")
        qs = apply_filters(
            request,
            _transcript_service().scoped(user=request.user, roles=get_user_roles(request)),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([transcript_to_dict(t) for t in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        # Gated at read (self/child); requesting ANOTHER student requires write.
        check_perm(request, "academics:read")
        data = read_json(request)
        student = StudentProfile.objects.filter(pk=_int_value(_require(data, "student"), "student")).first()
        # Uniform not-found for a missing student and an existing student outside
        # the caller's authority. A 400-vs-403 split is a tenant-wide ID oracle.
        if student is None or (
            not _is_self_or_child(request, student)
            and not has_permission_code(get_user_roles(request), "academics:write")
        ):
            raise NotFoundException(code="not_found")
        term = None
        if data.get("term") is not None:
            term = Term.objects.filter(pk=_int_value(data["term"], "term")).first()
            if term is None:
                raise _reject("term", "Term does not exist.")
        transcript = _transcript_service().request(student=student, term=term, requested_by=request.user)
        return success({"id": transcript.id, "status": transcript.status}, status=202)
    return _method_not_allowed()


@csrf_exempt
@require_auth
def transcript_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:read")
    transcript = (
        _transcript_service().scoped(user=request.user, roles=get_user_roles(request)).filter(pk=pk).first()
    )
    if transcript is None:
        raise NotFoundException(code="not_found")
    return success(transcript_to_dict(transcript))


# --- reports (staff-only aggregates) ---------------------------------------


def _assert_report_access(request: HttpRequest) -> None:
    if request.user.is_superuser or (get_user_roles(request) & _REPORT_ROLES):
        return
    raise PermissionException("Honor roll and warnings are staff-only.", code="forbidden")


# Honor-roll / warnings are ordered top-N reports (by score); hard-cap the row count so a
# large term can't materialize an unbounded list into one response (every other list here
# paginates — these return a bare list on purpose, so cap with a SQL LIMIT slice instead).
_HONOR_WARNING_CAP = 1000


@csrf_exempt
@require_auth
def honor_roll_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:read")
    _assert_report_access(request)
    term_id = _require_int_qparam(request, "term")
    grades = _grade_service().honor_roll(term_id=term_id, user=request.user, roles=get_user_roles(request))
    return success([grade_to_dict(g) for g in grades[:_HONOR_WARNING_CAP]])


@csrf_exempt
@require_auth
def warnings_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:read")
    _assert_report_access(request)
    term_id = _require_int_qparam(request, "term")
    grades = _grade_service().warnings(term_id=term_id, user=request.user, roles=get_user_roles(request))
    return success([grade_to_dict(g) for g in grades[:_HONOR_WARNING_CAP]])
