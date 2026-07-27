"""Attendance endpoints — plain Django views over the layered architecture.

Role-scoped reads (a student sees only their own records; a parent their guardian-
linked children's; a teacher only their taught lessons'; a director/HoD all) — the
scoping is applied by the queryset, so an out-of-scope record is simply absent
(never a 403 that leaks existence). The mark endpoint accepts a JSON ARRAY of
entries and upserts them (teacher-scope + correction-window enforced in the service).
"""

from __future__ import annotations

import csv
import json
from typing import Any

from django.http import HttpRequest, HttpResponseBase, StreamingHttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from apps.attendance.dto.attendance_dto import MarkEntryDTO
from apps.attendance.interfaces.services import IAttendanceService
from apps.attendance.models import AttendanceRecord
from apps.attendance.presenters import record_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import int_field, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import error, paginated, success

_RESOURCE = "attendance"
_VALID_STATUSES = {value for value, _label in AttendanceRecord.Status.choices}


def _service() -> IAttendanceService:
    return container.resolve(IAttendanceService)  # type: ignore[type-abstract]


def _roles(request: HttpRequest) -> set[str]:
    req: Any = request  # perm helpers are duck-typed on .user (typed Request upstream)
    return get_user_roles(req)


# --- records (read-only, role-scoped) --------------------------------------
@csrf_exempt
@require_auth
def records_collection_view(request: HttpRequest) -> HttpResponseBase:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    qs = _service().scoped_records(user=request.user, roles=_roles(request))
    # student/lesson/status are direct fields (apply_filters coerces + 400s a bad FK);
    # cohort + date range map onto the lesson relation, handled explicitly below.
    qs = apply_filters(
        request,
        qs,
        filter_fields=("student", "lesson", "status"),
        ordering_fields=("created_at", "marked_at"),
    )
    cohort = _optional_int(request, "cohort")
    if cohort is not None:
        qs = qs.filter(lesson__cohort_id=cohort)
    date_from = _parse_dt(request, "date_from")
    if date_from is not None:
        qs = qs.filter(lesson__starts_at__gte=date_from)
    date_to = _parse_dt(request, "date_to")
    if date_to is not None:
        qs = qs.filter(lesson__starts_at__lte=date_to)
    items, total, page, size = paginate(request, qs)
    return paginated([record_to_dict(r) for r in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def record_detail_view(request: HttpRequest, pk: int) -> HttpResponseBase:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    record = _service().get_record(user=request.user, roles=_roles(request), pk=pk)
    if record is None:
        raise NotFoundException(code="not_found")  # scoped out -> 404, no existence leak
    return success(record_to_dict(record))


# --- mark (upsert; teacher-scoped) -----------------------------------------
@csrf_exempt
@require_auth
def mark_view(request: HttpRequest, lesson_id: int) -> HttpResponseBase:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    lesson = _service().get_lesson(lesson_id=lesson_id)
    if lesson is None:
        raise NotFoundException(code="not_found")  # incl. a cross-tenant lesson id
    entries = _mark_entries(request)
    result = _service().mark(lesson=lesson, entries=entries, actor=request.user)
    return success(
        {
            "created": result["created"],
            "updated": result["updated"],
            "records": [record_to_dict(r) for r in result["records"]],
        }
    )


# --- summary + dashboard + export ------------------------------------------
@csrf_exempt
@require_auth
def summary_view(request: HttpRequest) -> HttpResponseBase:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    data = _service().term_summary(
        user=request.user,
        roles=_roles(request),
        student_id=_require_int(request, "student"),
        term_id=_require_int(request, "term"),
    )
    return success(data)


@csrf_exempt
@require_auth
def dashboard_view(request: HttpRequest, cohort_id: int) -> HttpResponseBase:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    service = _service()
    service.authorize_dashboard(user=request.user, roles=_roles(request), cohort_id=cohort_id)
    data = service.cohort_dashboard(
        cohort_id=cohort_id,
        date_from=_parse_dt(request, "date_from"),
        date_to=_parse_dt(request, "date_to"),
    )
    return success(data)


@csrf_exempt
@require_auth
def export_view(request: HttpRequest) -> HttpResponseBase:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    qs = _service().scoped_records(user=request.user, roles=_roles(request))
    # Coerce optional FK filters to int; a non-integer (?cohort=abc) otherwise reaches
    # the DB int cast and raises a 500 instead of a clean 400.
    cohort = _optional_int(request, "cohort")
    term = _optional_int(request, "term")
    if cohort is not None:
        qs = qs.filter(lesson__cohort_id=cohort)
    if term is not None:
        qs = qs.filter(lesson__term_id=term)
    qs = qs.select_related("marked_by").order_by("lesson__starts_at", "student_id")

    response = StreamingHttpResponse(_csv_rows(qs), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="attendance.csv"'
    return response


# --- helpers ---------------------------------------------------------------
def _mark_entries(request: HttpRequest) -> list[MarkEntryDTO]:
    raw = _read_json_array(request)
    entries: list[MarkEntryDTO] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationException(
                "Each attendance entry must be an object.",
                code="validation_error",
                fields={"entries": [f"Item {index} must be an object."]},
            )
        status_value = str_field(item, "status", max_length=20)
        if status_value not in _VALID_STATUSES:
            raise ValidationException(
                "Invalid attendance status.",
                code="validation_error",
                fields={"status": [f"Must be one of: {sorted(_VALID_STATUSES)}."]},
            )
        student_id = int_field(item, "student", required=True)
        entries.append(
            MarkEntryDTO(
                student_id=student_id,  # type: ignore[arg-type]  # required=True -> never None
                status=status_value,
                arrived_at=_entry_datetime(item, "arrived_at"),
                note=str_field(item, "note", max_length=500),
            )
        )
    return entries


def _read_json_array(request: HttpRequest) -> list:
    """The request body as a JSON array (``[]`` when empty). 400 on invalid JSON or a
    non-array body — the mark payload is a list of entries, unlike ``read_json``."""
    if not request.body:
        return []
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        raise ValidationException("Request body must be valid JSON.", code="invalid_json") from None
    if not isinstance(data, list):
        raise ValidationException(
            "Request body must be a JSON array of attendance entries.", code="invalid_json"
        )
    return data


def _entry_datetime(item: dict[str, Any], name: str):
    """Parse an optional per-entry ISO datetime; a malformed value is a clean 400
    (parse_datetime RAISES ValueError on a regex-valid-but-impossible date)."""
    raw = item.get(name)
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ValidationException(
            f"{name} must be an ISO 8601 datetime string.",
            code="validation_error",
            fields={name: ["Must be an ISO 8601 datetime string."]},
        )
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValidationException(
            f"{name} must be a valid ISO 8601 datetime.",
            code="validation_error",
            fields={name: ["Enter a valid ISO 8601 datetime."]},
        )
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _require_int(request: HttpRequest, name: str) -> int:
    raw = request.GET.get(name)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValidationException(
            f"Query parameter '{name}' is required and must be an integer.",
            code="invalid_query_param",
            fields={name: ["This query parameter is required."]},
        ) from exc


def _optional_int(request: HttpRequest, name: str) -> int | None:
    """None when absent; 400 when present but non-integer (so it never reaches a DB
    int cast as a 500)."""
    raw = request.GET.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationException(
            f"Query parameter '{name}' must be an integer.",
            code="invalid_query_param",
            fields={name: ["Must be an integer."]},
        ) from exc


def _parse_dt(request: HttpRequest, name: str):
    """Parse an optional `date_from`/`date_to` ISO datetime query param. Returns None
    when absent; raises a 400 on a malformed value so a bad input surfaces as the
    TD-18 envelope instead of an ORM-level 500 (parse_datetime RAISES ValueError on a
    regex-valid-but-impossible value)."""
    raw = request.GET.get(name)
    if not raw:
        return None
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValidationException(
            f"Query parameter '{name}' must be a valid ISO 8601 datetime.",
            code="invalid_query_param",
            fields={name: ["Enter a valid ISO 8601 datetime."]},
        )
    return parsed


def _csv_rows(records):
    writer = csv.writer(_Echo())
    yield writer.writerow(["date", "lesson", "student", "status", "marked_by"])
    for record in records.iterator():
        yield writer.writerow(
            [
                timezone.localdate(record.lesson.starts_at).isoformat(),
                record.lesson.title,
                record.student.get_full_name(),
                record.status,
                getattr(record.marked_by, "username", "") or ("auto" if record.auto_marked else ""),
            ]
        )


class _Echo:
    """Write-only file-like object that returns each row for StreamingHttpResponse."""

    def write(self, value: str) -> str:
        return value
