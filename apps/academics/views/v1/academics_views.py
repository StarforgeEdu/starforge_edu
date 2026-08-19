"""Academics HTTP views (layered, off DRF).

Subjects (CRUD), exams (CRUD + per-student results record/list/CSV-import/publish,
cohort-scoped so a teacher only reaches their own cohorts), read-only computed
grades, grade recompute, transcripts (async PDF), and the staff-only honor-roll /
academic-warning aggregates. Raw per-student results are gated at ``academics:write``
even on read, so a student/parent holding ``academics:read`` can't harvest scores.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt

from apps.academics.dto import ResultFieldError, validate_result_values
from apps.academics.interfaces.services import (
    IExamService,
    IExamTypeService,
    IGradeService,
    ISubjectService,
    ITranscriptService,
)
from apps.academics.models import Exam
from apps.academics.openapi_contracts import EXAM_RESULTS_CONTRACTS
from apps.academics.presenters import (
    exam_lifecycle_event_to_dict,
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
from core.http import bool_field, decimal_field, parse_bool, read_json, read_json_array
from core.listing import apply_filters, paginate, positive_int_filter
from core.openapi_contracts import openapi_contract
from core.permissions import get_user_roles
from core.ratelimit import check_rate
from core.responses import created, error, no_content, paginated, success
from core.scoping import (
    is_permission_unscoped,
    permission_membership_scope_q,
    permission_membership_scopes,
    request_permission_membership_allows,
)
from core.utils import current_schema

# Honor-roll / warnings are staff-facing aggregates (never exposed to the
# students/parents who also hold `academics:read`).
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


def _check_catalog_write(request: HttpRequest) -> None:
    """Catalogue mutations require this exact grant at organization scope."""
    check_perm(request, "academics:catalogue")
    if is_permission_unscoped(
        request,
        permission="academics:catalogue",
        account_kinds={"staff"},
    ):
        return
    raise PermissionException(
        "Organization-wide catalogue authority is required.",
        code="catalogue_scope_required",
    )


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


def _subject_changes(request: HttpRequest, *, partial: bool) -> dict[str, Any]:
    data = read_json(request)
    if not partial:
        missing = [field for field in ("name", "code") if field not in data]
        if missing:
            raise ValidationException(
                "Required fields are missing.",
                code="validation_error",
                fields={field: ["This field is required."] for field in missing},
            )
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
        _check_catalog_write(request)
        subject = _subject_service().create(data=_subject_create_data(request), actor=request.user)
        return created(subject_to_dict(subject))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def subject_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    if read:
        check_perm(request, "academics:read")
    else:
        _check_catalog_write(request)
    subject = _subject_service().get(pk=pk)
    if subject is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(subject_to_dict(subject))
    if request.method in ("PUT", "PATCH"):
        return success(
            subject_to_dict(
                _subject_service().update(
                    subject,
                    changes=_subject_changes(request, partial=request.method == "PATCH"),
                    actor=request.user,
                )
            )
        )
    if request.method == "DELETE":
        _subject_service().delete(subject, actor=request.user)
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
        _check_catalog_write(request)
        exam_type = _exam_type_service().create(
            data=_exam_type_create_data(request),
            actor=request.user,
        )
        return created(exam_type_to_dict(exam_type))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def exam_type_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    if read:
        check_perm(request, "academics:read")
    else:
        _check_catalog_write(request)
    exam_type = _exam_type_service().get(pk=pk)
    if exam_type is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(exam_type_to_dict(exam_type))
    if request.method in ("PUT", "PATCH"):
        return success(
            exam_type_to_dict(
                _exam_type_service().update(
                    exam_type,
                    changes=_exam_type_changes(request),
                    actor=request.user,
                )
            )
        )
    if request.method == "DELETE":
        _exam_type_service().delete(exam_type, actor=request.user)
        return no_content()
    return _method_not_allowed()


# --- exams -----------------------------------------------------------------


def _writable_cohort_ids(request: HttpRequest):
    """Cohorts covered by the exact membership granting academics:write."""
    from django.db.models import Q

    from apps.academics.selectors import _cohorts_taught_by

    user = request.user
    roles = get_user_roles(request)
    if is_permission_unscoped(
        request,
        permission="academics:write",
        account_kinds={"staff"},
    ):
        return None
    staff_scope = permission_membership_scope_q(
        roles=roles,
        permission="academics:write",
        branch_field="branch_id",
        department_field="department_id",
        account_kinds={"staff"},
    )
    teacher_scope = Q(pk__in=[])
    if permission_membership_scopes(
        roles=roles,
        permission="academics:write",
        account_kinds={"teacher"},
    ):
        teacher_scope = permission_membership_scope_q(
            roles=roles,
            permission="academics:write",
            branch_field="branch_id",
            department_field="department_id",
            account_kinds={"teacher"},
        ) & Q(pk__in=_cohorts_taught_by(user))
    return set(Cohort.objects.filter(staff_scope | teacher_scope).values_list("pk", flat=True))


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


def _exam_changes_data(data: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    if not partial:
        required = ("subject", "cohort", "term", "exam_type", "title", "exam_date")
        missing = [field for field in required if field not in data]
        if missing:
            raise ValidationException(
                "Required fields are missing.",
                code="validation_error",
                fields={field: ["This field is required."] for field in missing},
            )
    changes: dict[str, Any] = {}
    if "subject" in data:
        changes["subject"] = _int_value(data["subject"], "subject")
    if "cohort" in data:
        changes["cohort"] = _int_value(data["cohort"], "cohort")
    if "term" in data:
        changes["term"] = _int_value(data["term"], "term")
    if "exam_type" in data:
        changes["exam_type"] = (
            None if data["exam_type"] is None else _int_value(data["exam_type"], "exam_type")
        )
    if "title" in data:
        changes["title"] = _str_value(data["title"], "title", max_length=200)
    if "exam_date" in data:
        changes["exam_date"] = _date_value(data["exam_date"], "exam_date")
    _add_optional_decimals(data, changes)
    return changes


def _exam_changes(request: HttpRequest, *, partial: bool) -> dict[str, Any]:
    return _exam_changes_data(read_json(request), partial=partial)


def _add_optional_decimals(data: dict[str, Any], out: dict[str, Any]) -> None:
    """max_score (6,2) / weight (4,3) are optional (model defaults 100/1). Present
    with a value → validate; explicitly null/blank on a NOT-NULL column → 400."""
    for field, digits, places in (("max_score", 6, 2), ("weight", 4, 3)):
        if field not in data:
            continue
        value = decimal_field(data, field, max_digits=digits, decimal_places=places)
        if value is None:
            raise _reject(field, "This field may not be null.")
        if value <= 0:
            raise _reject(field, "This field must be greater than zero.")
        out[field] = value


@csrf_exempt
@require_auth
def exams_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "academics:read")
        qs = _exam_service().scoped(
            user=request.user,
            roles=get_user_roles(request),
            permission="academics:read",
        )
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
            data=_exam_create_data(request),
            writable_cohort_ids=_writable_cohort_ids(request),
            created_by=request.user,
        )
        return created(exam_to_dict(exam))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def exams_overview_view(request: HttpRequest) -> HttpResponse:
    """Exact assessment readiness and schedule aggregates for one visible scope."""
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:read")
    exams = _exam_service().scoped(
        user=request.user,
        roles=get_user_roles(request),
        permission="academics:read",
    )
    branch_id = positive_int_filter(request, "branch")
    if branch_id is not None:
        exams = exams.filter(cohort__branch_id=branch_id)

    today = timezone.localdate()
    window_end = today + timedelta(days=13)
    totals = exams.aggregate(
        total_exams=Count("id"),
        published_exams=Count(
            "id",
            filter=Q(is_published=True, requires_republish=False),
        ),
        drafts=Count(
            "id",
            filter=Q(is_published=False, requires_republish=False),
        ),
        corrections_due=Count("id", filter=Q(requires_republish=True)),
        next_14_days=Count(
            "id",
            filter=Q(exam_date__gte=today, exam_date__lte=window_end),
        ),
        subjects_used=Count("subject_id", distinct=True),
    )

    schedule = list(exams.filter(exam_date__gte=today).order_by("exam_date", "id")[:8])
    schedule_kind = "upcoming"
    if not schedule:
        schedule = list(exams.filter(exam_date__lt=today).order_by("-exam_date", "-id")[:8])
        schedule_kind = "recent"
    attention = list(
        exams.filter(Q(requires_republish=True) | Q(is_published=False)).order_by(
            "-requires_republish", "exam_date", "id"
        )[:6]
    )
    subject_distribution = list(
        exams.values("subject_id", "subject__name")
        .annotate(value=Count("id"))
        .order_by("-value", "subject__name")[:7]
    )
    type_distribution = list(
        exams.values("exam_type_id", "exam_type__name")
        .annotate(value=Count("id"))
        .order_by("-value", "exam_type__name")[:7]
    )
    return success(
        {
            **totals,
            "branch": branch_id,
            "as_of": today.isoformat(),
            "window_end": window_end.isoformat(),
            "schedule_kind": schedule_kind,
            "schedule": [exam_to_dict(exam) for exam in schedule],
            "attention": [exam_to_dict(exam) for exam in attention],
            "subject_distribution": [
                {
                    "id": row["subject_id"],
                    "label": row["subject__name"] or "Subject not recorded",
                    "value": row["value"],
                }
                for row in subject_distribution
            ],
            "type_distribution": [
                {
                    "id": row["exam_type_id"],
                    "label": row["exam_type__name"] or "Type not recorded",
                    "value": row["value"],
                }
                for row in type_distribution
            ],
        }
    )


def _get_exam_in_scope(request: HttpRequest, pk: int, *, permission: str) -> Exam:
    exam = _exam_service().get_scoped(
        pk=pk,
        user=request.user,
        roles=get_user_roles(request),
        permission=permission,
    )
    if exam is None:
        raise NotFoundException(code="not_found")
    return exam


@csrf_exempt
@require_auth
def exam_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "academics:read" if read else "academics:write")
    exam = _get_exam_in_scope(
        request,
        pk,
        permission="academics:read" if read else "academics:write",
    )
    if read:
        return success(exam_to_dict(exam))
    if request.method in ("PUT", "PATCH"):
        exam = _exam_service().update(
            exam,
            changes=_exam_changes(request, partial=request.method == "PATCH"),
            writable_cohort_ids=_writable_cohort_ids(request),
            actor=request.user,
        )
        return success(exam_to_dict(exam))
    if request.method == "DELETE":
        _exam_service().delete(exam, actor=request.user)
        return no_content()
    return _method_not_allowed()


def _parse_result_items(
    raw: Any,
    *,
    exam: Exam,
    root: str = "rows",
    max_score=None,
) -> list[dict]:
    if not isinstance(raw, list):
        raise _reject(root, "This field must be an array.")
    from apps.academics.services import MAX_IMPORT_ROWS

    if len(raw) > MAX_IMPORT_ROWS:
        raise _reject(root, f"Too many rows (max {MAX_IMPORT_ROWS}).")
    parsed: list[tuple[int, int | None, str | None, Any]] = []
    student_ids: set[int] = set()
    student_codes: set[str] = set()
    for index, item in enumerate(raw):
        field = f"{root}[{index}]"
        if not isinstance(item, dict):
            raise _reject(field, "Each row must be an object.")
        unknown = set(item) - {"student", "student_code", "score", "note", "components"}
        if unknown:
            raise _reject(
                field,
                f"Unknown fields: {', '.join(sorted(unknown))}.",
            )
        has_id = item.get("student") is not None
        has_code = item.get("student_code") is not None
        if has_id == has_code:
            raise _reject(field, "Provide exactly one of student or student_code.")
        student_id = None
        student_code = None
        if has_id:
            student_id = _int_value(item["student"], f"{field}.student")
            student_ids.add(student_id)
        else:
            student_code = _str_value(
                item["student_code"],
                f"{field}.student_code",
                max_length=32,
            )
            student_codes.add(student_code)
        if "score" not in item:
            raise _reject(f"{field}.score", "This field is required.")
        try:
            validation_kwargs = {}
            if "components" in item:
                validation_kwargs["components"] = item["components"]
            values = validate_result_values(
                score=item["score"],
                note=item.get("note", ""),
                max_score=max_score if max_score is not None else exam.max_score,
                **validation_kwargs,
            )
        except ResultFieldError as exc:
            raise _reject(f"{field}.{exc.field}", exc.message) from None
        parsed.append((index, student_id, student_code, values))

    students_by_id = StudentProfile.objects.in_bulk(student_ids)
    students_by_code = StudentProfile.objects.in_bulk(student_codes, field_name="student_id")
    rows: list[dict] = []
    seen_profiles: set[int] = set()
    for index, student_id, student_code, values in parsed:
        student = (
            students_by_id.get(student_id)
            if student_id is not None
            else students_by_code.get(student_code or "")
        )
        if student is None:
            identifier = "student" if student_id is not None else "student_code"
            raise _reject(f"{root}[{index}].{identifier}", "Student does not exist.")
        if student.pk in seen_profiles:
            raise _reject(f"{root}[{index}]", "A student may appear only once per batch.")
        seen_profiles.add(student.pk)
        row = {"student": student, "score": values.score, "note": values.note}
        if values.components is not None:
            row["components"] = list(values.components)
        rows.append(row)
    return rows


@openapi_contract(
    path="/api/v1/academics/exams/{pk}/results/",
    operations=EXAM_RESULTS_CONTRACTS,
)
@csrf_exempt
@require_auth
def exam_results_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD", "POST"):
        return _method_not_allowed()
    # Raw per-student results are staff/teacher-only on READ and write.
    check_perm(request, "academics:write")
    exam = _get_exam_in_scope(request, pk, permission="academics:write")
    if request.method in ("GET", "HEAD"):
        items, total, page, size = paginate(request, _exam_service().results_for(exam))
        return paginated(
            [exam_result_to_dict(r) for r in items],
            total=total,
            page=page,
            page_size=size,
        )
    # Result writes are a top-level array rather than the API's usual object DTO.
    rows = _parse_result_items(read_json_array(request), exam=exam)
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
    exam = _get_exam_in_scope(request, pk, permission="academics:write")
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
    exam = _get_exam_in_scope(request, pk, permission="academics:write")
    data = read_json(request)
    expected_version = _int_value(
        _require(data, "expected_version"),
        "expected_version",
    )
    if expected_version < 1:
        raise _reject("expected_version", "This field must be a positive integer.")
    confirmed = parse_bool(_require(data, "confirmed"), "confirmed")
    exam, readiness = _exam_service().publish(
        exam=exam,
        actor=request.user,
        expected_version=expected_version,
        confirmed=confirmed,
    )
    return success({"exam": exam_to_dict(exam), "readiness": readiness.as_dict()})


@csrf_exempt
@require_auth
def exam_readiness_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:write")
    exam = _get_exam_in_scope(request, pk, permission="academics:write")
    return success(_exam_service().readiness(exam=exam).as_dict())


@csrf_exempt
@require_auth
def exam_correction_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "academics:write")
    exam = _get_exam_in_scope(request, pk, permission="academics:write")
    data = read_json(request)
    unknown = set(data) - {"expected_version", "reason", "changes", "results"}
    if unknown:
        raise _reject("body", f"Unknown fields: {', '.join(sorted(unknown))}.")
    expected_version = _int_value(
        _require(data, "expected_version"),
        "expected_version",
    )
    if expected_version < 1:
        raise _reject("expected_version", "This field must be a positive integer.")
    reason = _str_value(
        _require(data, "reason"),
        "reason",
        max_length=500,
    )
    change_data = data.get("changes", {})
    if not isinstance(change_data, dict):
        raise _reject("changes", "This field must be an object.")
    allowed_changes = {
        "subject",
        "cohort",
        "term",
        "exam_type",
        "title",
        "exam_date",
        "max_score",
        "weight",
    }
    unknown_changes = set(change_data) - allowed_changes
    if unknown_changes:
        raise _reject(
            "changes",
            f"Unknown fields: {', '.join(sorted(unknown_changes))}.",
        )
    parsed_changes = _exam_changes_data(change_data, partial=True)
    rows = _parse_result_items(
        data.get("results", []),
        exam=exam,
        root="results",
        max_score=parsed_changes.get("max_score", exam.max_score),
    )
    corrected, event = _exam_service().correct(
        exam=exam,
        changes=parsed_changes,
        rows=rows,
        reason=reason,
        expected_version=expected_version,
        writable_cohort_ids=_writable_cohort_ids(request),
        actor=request.user,
    )
    return success(
        {
            "exam": exam_to_dict(corrected),
            "correction": exam_lifecycle_event_to_dict(event),
        }
    )


@csrf_exempt
@require_auth
def exam_history_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:write")
    exam = _get_exam_in_scope(request, pk, permission="academics:write")
    items, total, page, size = paginate(request, _exam_service().history(exam=exam))
    return paginated(
        [exam_lifecycle_event_to_dict(event) for event in items],
        total=total,
        page=page,
        page_size=size,
    )


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
        raise PermissionException("You may only recompute grades for cohorts you teach.", code="forbidden")
    grades = _grade_service().recompute(cohort=cohort, subject=subject, term=term, publish=publish)
    return success({"recomputed": len(grades)})


# --- transcripts -----------------------------------------------------------


def _is_self_or_child(request: HttpRequest, student) -> bool:
    user: Any = request.user  # @require_auth guarantees an authenticated User
    department_id = student.current_cohort.department_id if student.current_cohort_id else None
    if student.user_id == user.id and request_permission_membership_allows(
        request,
        permission="academics:read",
        branch_id=student.branch_id,
        department_id=department_id,
        account_kinds={"student"},
    ):
        return True
    return (
        request_permission_membership_allows(
            request,
            permission="academics:read",
            branch_id=student.branch_id,
            department_id=department_id,
            account_kinds={"parent"},
        )
        and Guardian.objects.filter(
            student=student,
            parent__user=user,
            revoked_at__isnull=True,
        ).exists()
    )


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
        student = (
            StudentProfile.objects.select_related("current_cohort")
            .filter(pk=_int_value(_require(data, "student"), "student"))
            .first()
        )
        # Uniform not-found for a missing student and an existing student outside
        # the caller's authority. A 400-vs-403 split is a tenant-wide ID oracle.
        staff_scoped = False
        if student is not None:
            current_cohort = student.current_cohort
            staff_scoped = request_permission_membership_allows(
                request,
                permission="academics:write",
                branch_id=student.branch_id,
                department_id=current_cohort.department_id if current_cohort else None,
                account_kinds={"staff", "teacher"},
            )
        if student is None or (not _is_self_or_child(request, student) and not staff_scoped):
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
    if request.user.is_superuser or permission_membership_scopes(
        roles=get_user_roles(request),
        permission="academics:read",
        account_kinds={"staff", "teacher"},
    ):
        return
    raise PermissionException("Honor roll and warnings are staff-only.", code="forbidden")


@csrf_exempt
@require_auth
def honor_roll_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:read")
    _assert_report_access(request)
    term_id = _require_int_qparam(request, "term")
    grades = _grade_service().honor_roll(term_id=term_id, user=request.user, roles=get_user_roles(request))
    items, total, page, size = paginate(request, grades)
    return paginated([grade_to_dict(g) for g in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def warnings_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "academics:read")
    _assert_report_access(request)
    term_id = _require_int_qparam(request, "term")
    grades = _grade_service().warnings(term_id=term_id, user=request.user, roles=get_user_roles(request))
    items, total, page, size = paginate(request, grades)
    return paginated([grade_to_dict(g) for g in items], total=total, page=page, page_size=size)
