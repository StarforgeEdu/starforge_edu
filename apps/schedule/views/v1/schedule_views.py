"""Schedule HTTP views (layered, off DRF).

Staff CRUD for terms / time-slots / lesson-types / recurrence-rules, plus the
read-only scoped lesson feed with cancel/move actions and the personal iCal feed.
All staff endpoints gate on ``schedule:read`` / ``schedule:write``; the iCal feed
(``ical/<token>/``) is PUBLIC — authenticated by a signed, tenant-bound token in
the URL, never by a session — so it carries no ``@require_auth``.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time
from django.views.decorators.csrf import csrf_exempt

from apps.schedule.interfaces.services import (
    ILessonService,
    ILessonTypeService,
    IRecurrenceRuleService,
    ITermService,
    ITimeSlotService,
)
from apps.schedule.presenters import (
    lesson_to_dict,
    lesson_type_to_dict,
    rule_to_dict,
    term_to_dict,
    time_slot_to_dict,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, parse_bool, read_json
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.responses import created, error, no_content, paginated, success
from core.scoping import assert_branch_id_in_scope, assert_in_branch_scope

# --- service accessors -----------------------------------------------------


def _term_service() -> ITermService:
    return container.resolve(ITermService)  # type: ignore[type-abstract]


def _slot_service() -> ITimeSlotService:
    return container.resolve(ITimeSlotService)  # type: ignore[type-abstract]


def _lesson_type_service() -> ILessonTypeService:
    return container.resolve(ILessonTypeService)  # type: ignore[type-abstract]


def _rule_service() -> IRecurrenceRuleService:
    return container.resolve(IRecurrenceRuleService)  # type: ignore[type-abstract]


def _lesson_service() -> ILessonService:
    return container.resolve(ILessonService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


# --- value validators (never-500 on bad input) -----------------------------

# 100 years in minutes: a generous bound on a bulk-reschedule shift that still keeps
# datetime.timedelta and lesson date arithmetic far from OverflowError (a raw 500).
_MAX_SHIFT_MINUTES = 100 * 366 * 24 * 60


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require(data: dict[str, Any], name: str) -> Any:
    if name not in data or data[name] is None:
        raise _reject(name, "This field is required.")
    return data[name]


def _str_value(
    raw: Any, name: str, *, max_length: int | None = None, allow_blank: bool = False, strip: bool = True
) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    value = raw.strip() if strip else raw
    if "\x00" in value:
        raise _reject(name, "Null characters are not allowed.")
    if not value.strip() and not allow_blank:
        raise _reject(name, "This field may not be blank.")
    if max_length is not None and len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _int_value(raw: Any, name: str, *, min_value: int | None = None, max_value: int | None = None) -> int:
    if isinstance(raw, bool):
        raise _reject(name, "A valid integer is required.")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            raise _reject(name, "A valid integer is required.") from None
    else:
        raise _reject(name, "A valid integer is required.")
    if min_value is not None and value < min_value:
        raise _reject(name, f"Ensure this value is greater than or equal to {min_value}.")
    if max_value is not None and value > max_value:
        raise _reject(name, f"Ensure this value is less than or equal to {max_value}.")
    return value


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


def _time_value(raw: Any, name: str):
    if not isinstance(raw, str):
        raise _reject(name, "Enter a valid time (HH:MM[:SS]).")
    try:
        parsed = parse_time(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise _reject(name, "Enter a valid time (HH:MM[:SS]).")
    return parsed


def _datetime_value(raw: Any, name: str):
    if not isinstance(raw, str):
        raise _reject(name, "Enter a valid ISO 8601 datetime.")
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise _reject(name, "Enter a valid ISO 8601 datetime.")
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _query_datetime(raw: str, name: str):
    """Parse an ISO datetime query param; a malformed value is a clean 400
    (parse_datetime RAISES ValueError on a regex-valid-but-impossible date)."""
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValidationException(
            "Invalid query parameter.",
            code="invalid_query_param",
            fields={name: ["Enter a valid ISO 8601 datetime."]},
        )
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


# --- terms -----------------------------------------------------------------


def _term_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    return {
        "name": _str_value(_require(data, "name"), "name", max_length=100),
        "academic_year": _str_value(_require(data, "academic_year"), "academic_year", max_length=9),
        "start_date": _date_value(_require(data, "start_date"), "start_date"),
        "end_date": _date_value(_require(data, "end_date"), "end_date"),
        "is_current": bool_field(data, "is_current", default=False),
    }


def _term_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "name" in data:
        changes["name"] = _str_value(data["name"], "name", max_length=100)
    if "academic_year" in data:
        changes["academic_year"] = _str_value(data["academic_year"], "academic_year", max_length=9)
    if "start_date" in data:
        changes["start_date"] = _date_value(data["start_date"], "start_date")
    if "end_date" in data:
        changes["end_date"] = _date_value(data["end_date"], "end_date")
    if "is_current" in data:
        changes["is_current"] = parse_bool(data["is_current"], "is_current")
    return changes


@csrf_exempt
@require_auth
def terms_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "schedule:read")
        qs = apply_filters(
            request,
            _term_service().list_terms(),
            filter_fields=("academic_year", "is_current"),
            search_fields=("name", "academic_year"),
            ordering_fields=("start_date", "name"),
            default_ordering="-start_date",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([term_to_dict(t) for t in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "schedule:write")
        term = _term_service().create(data=_term_create_data(request))
        return created(term_to_dict(term))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def term_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "schedule:read" if read else "schedule:write")
    term = _term_service().get(pk=pk)
    if term is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(term_to_dict(term))
    if request.method in ("PUT", "PATCH"):
        return success(term_to_dict(_term_service().update(term, changes=_term_changes(request))))
    if request.method == "DELETE":
        _term_service().delete(term)
        return no_content()
    return _method_not_allowed()


# --- time slots (branch object-scope: create + detail, NOT list) -----------


def _slot_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    return {
        "branch_id": _int_value(_require(data, "branch"), "branch"),
        "name": _str_value(_require(data, "name"), "name", max_length=50),
        "start_time": _time_value(_require(data, "start_time"), "start_time"),
        "end_time": _time_value(_require(data, "end_time"), "end_time"),
        "order": _int_value(data.get("order", 0), "order", min_value=0),
    }


def _slot_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "branch" in data:
        changes["branch_id"] = _int_value(data["branch"], "branch")
    if "name" in data:
        changes["name"] = _str_value(data["name"], "name", max_length=50)
    if "start_time" in data:
        changes["start_time"] = _time_value(data["start_time"], "start_time")
    if "end_time" in data:
        changes["end_time"] = _time_value(data["end_time"], "end_time")
    if "order" in data:
        changes["order"] = _int_value(data["order"], "order", min_value=0)
    return changes


@csrf_exempt
@require_auth
def time_slots_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "schedule:read")
        qs = apply_filters(
            request,
            _slot_service().list_slots(),
            filter_fields=("branch",),
            ordering_fields=("order", "start_time"),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([time_slot_to_dict(s) for s in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "schedule:write")
        data = _slot_create_data(request)
        assert_branch_id_in_scope(request, data["branch_id"])
        slot = _slot_service().create(data=data)
        return created(time_slot_to_dict(slot))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def time_slot_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "schedule:read" if read else "schedule:write")
    slot = _slot_service().get(pk=pk)
    if slot is None:
        raise NotFoundException(code="not_found")
    assert_in_branch_scope(request, slot)
    if read:
        return success(time_slot_to_dict(slot))
    if request.method in ("PUT", "PATCH"):
        changes = _slot_changes(request)
        if "branch_id" in changes:
            assert_branch_id_in_scope(request, changes["branch_id"])
        return success(time_slot_to_dict(_slot_service().update(slot, changes=changes)))
    if request.method == "DELETE":
        _slot_service().delete(slot)
        return no_content()
    return _method_not_allowed()


# --- lesson types ----------------------------------------------------------


def _lesson_type_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    out: dict[str, Any] = {
        "name": _str_value(_require(data, "name"), "name", max_length=64),
        "color": _str_value(data.get("color", ""), "color", max_length=16, allow_blank=True),
        "is_active": bool_field(data, "is_active", default=True),
    }
    if data.get("slug"):
        out["slug"] = _str_value(data["slug"], "slug", max_length=64)
    return out


def _lesson_type_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "name" in data:
        changes["name"] = _str_value(data["name"], "name", max_length=64)
    if "slug" in data:
        changes["slug"] = _str_value(data["slug"], "slug", max_length=64)
    if "color" in data:
        changes["color"] = _str_value(data["color"], "color", max_length=16, allow_blank=True)
    if "is_active" in data:
        changes["is_active"] = parse_bool(data["is_active"], "is_active")
    return changes


@csrf_exempt
@require_auth
def lesson_types_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "schedule:read")
        qs = apply_filters(
            request,
            _lesson_type_service().list_types(),
            filter_fields=("is_active",),
            search_fields=("name", "slug"),
            ordering_fields=("name",),
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([lesson_type_to_dict(lt) for lt in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "schedule:write")
        lt = _lesson_type_service().create(data=_lesson_type_create_data(request))
        return created(lesson_type_to_dict(lt))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def lesson_type_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "schedule:read" if read else "schedule:write")
    lt = _lesson_type_service().get(pk=pk)
    if lt is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(lesson_type_to_dict(lt))
    if request.method in ("PUT", "PATCH"):
        return success(
            lesson_type_to_dict(_lesson_type_service().update(lt, changes=_lesson_type_changes(request)))
        )
    if request.method == "DELETE":
        _lesson_type_service().delete(lt)
        return no_content()
    return _method_not_allowed()


# --- recurrence rules ------------------------------------------------------


def _rule_write_data(request: HttpRequest, *, partial: bool) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    # required FKs (int); explicit null on a NOT-NULL FK -> 400 via _int_value
    for field in ("term", "cohort", "teacher"):
        if field in data:
            changes[field] = _int_value(data[field], field)
        elif not partial:
            raise _reject(field, "This field is required.")
    # required scalars
    scalar_specs = (
        ("title", lambda v: _str_value(v, "title", max_length=200)),
        ("rrule", lambda v: _str_value(v, "rrule", strip=False)),
        ("start_date", lambda v: _date_value(v, "start_date")),
        ("end_date", lambda v: _date_value(v, "end_date")),
        ("start_time", lambda v: _time_value(v, "start_time")),
        ("end_time", lambda v: _time_value(v, "end_time")),
    )
    for name, conv in scalar_specs:
        if name in data:
            changes[name] = conv(data[name])
        elif not partial:
            raise _reject(name, "This field is required.")
    # nullable FKs
    for field in ("room", "lesson_type"):
        if field in data:
            changes[field] = None if data[field] is None else _int_value(data[field], field)
    # is_active is optional on create (the model BooleanField default=True applies when
    # omitted) and preserved when omitted on update — never force it, or a PUT that drops
    # is_active would silently reactivate a deactivated rule and re-materialize its lessons
    # (the old DRF ModelSerializer dropped an omitted is_active via SkipField).
    if "is_active" in data:
        changes["is_active"] = parse_bool(data["is_active"], "is_active")
    return changes


def _assert_rule_branch_scope(request: HttpRequest, changes: dict[str, Any]) -> None:
    """A RecurrenceRule has no branch column of its own — its branch is implied by its
    cohort/teacher/room FKs. A schedule:write holder may only author rules within their
    OWN branch, and those FKs must all resolve to ONE branch. Without this a branch-A
    writer could POST a rule naming branch-B's cohort/teacher/room (resolved by bare pk),
    materializing lessons + downstream attendance/absence-deduction rows in a branch they
    don't control, and use the endpoint as a cross-branch pk existence oracle. Mirrors the
    TimeSlot assert_branch_id_in_scope guard in this same file."""
    from apps.cohorts.models import Cohort
    from apps.org.models import Room
    from apps.teachers.models import TeacherProfile

    branches: set[int | None] = set()
    for field, model in (("cohort", Cohort), ("teacher", TeacherProfile), ("room", Room)):
        if field not in changes or changes[field] is None:
            continue
        obj = model.objects.filter(pk=changes[field]).first()
        if obj is None:
            raise _reject(field, f"{field} does not exist.")
        assert_branch_id_in_scope(request, obj.branch_id)
        branches.add(obj.branch_id)
    if len(branches) > 1:
        raise _reject("cohort", "cohort, teacher and room must belong to the same branch.")


@csrf_exempt
@require_auth
def rules_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "schedule:read")
        qs = apply_filters(
            request,
            _rule_service().scoped(user=request.user, roles=get_user_roles(request)),
            filter_fields=("term", "cohort", "teacher", "is_active"),
            ordering_fields=("created_at",),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([rule_to_dict(r) for r in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "schedule:write")
        data = _rule_write_data(request, partial=False)
        _assert_rule_branch_scope(request, data)
        rule = _rule_service().create(data=data, created_by=request.user)
        return created(rule_to_dict(rule))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def rule_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "schedule:read" if read else "schedule:write")
    rule = (
        _rule_service().get_scoped(pk=pk, user=request.user, roles=get_user_roles(request))
        if read
        else _rule_service().get(pk=pk)
    )
    if rule is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(rule_to_dict(rule))
    # A branch-scoped writer may only mutate a rule in their own branch (the rule's
    # branch is its cohort's branch) — else a bare-pk detail fetch is a cross-branch
    # write/delete + existence oracle.
    assert_branch_id_in_scope(request, rule.cohort.branch_id)
    if request.method in ("PUT", "PATCH"):
        changes = _rule_write_data(request, partial=(request.method == "PATCH"))
        _assert_rule_branch_scope(request, changes)
        return success(rule_to_dict(_rule_service().update(rule, changes=changes)))
    if request.method == "DELETE":
        _rule_service().delete(rule)
        return no_content()
    return _method_not_allowed()


@csrf_exempt
@require_auth
def rule_bulk_reschedule_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "schedule:write")
    rule = _rule_service().get(pk=pk)
    if rule is None:
        raise NotFoundException(code="not_found")
    # Object-level branch scope, same as rule_detail_view's mutating verbs: bulk-reschedule
    # shifts every lesson of the rule (and notifies its cohort), so a branch-scoped writer
    # must not drive it on another branch's rule via a bare-pk fetch (cross-branch mass
    # write + existence oracle). The rule's branch is its cohort's branch.
    assert_branch_id_in_scope(request, rule.cohort.branch_id)
    data = read_json(request)
    # Bound the shift so an absurd value can't overflow datetime.timedelta / a lesson's
    # date arithmetic into a raw 500 (100 years in minutes — far beyond any real reschedule).
    shift = _int_value(
        _require(data, "shift_minutes"),
        "shift_minutes",
        min_value=-_MAX_SHIFT_MINUTES,
        max_value=_MAX_SHIFT_MINUTES,
    )
    moved = _rule_service().bulk_reschedule(rule, shift_minutes=shift, actor=request.user)
    return success({"moved_count": moved})


# --- lessons (read-only scoped feed + cancel/move actions) -----------------


def _apply_lesson_date_range(request: HttpRequest, qs):
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    if date_from:
        qs = qs.filter(starts_at__gte=_query_datetime(date_from, "date_from"))
    if date_to:
        qs = qs.filter(starts_at__lte=_query_datetime(date_to, "date_to"))
    return qs


@csrf_exempt
@require_auth
def lessons_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "schedule:read")
        qs = _lesson_service().scoped(user=request.user, roles=get_user_roles(request))
        qs = _apply_lesson_date_range(request, qs)
        qs = apply_filters(
            request,
            qs,
            filter_fields=("cohort", "teacher", "room", "status", "term"),
            ordering_fields=("starts_at",),
            default_ordering="starts_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([lesson_to_dict(lf) for lf in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        # Lessons are materialized from RecurrenceRule occurrences, never POSTed.
        check_perm(request, "schedule:write")
        return error("Lessons are generated from recurrence rules.", code="method_not_allowed", status=405)
    return _method_not_allowed()


@csrf_exempt
@require_auth
def lesson_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "schedule:read")
    lesson = _lesson_service().get_scoped(pk=pk, user=request.user, roles=get_user_roles(request))
    if lesson is None:
        raise NotFoundException(code="not_found")
    return success(lesson_to_dict(lesson))


@csrf_exempt
@require_auth
def lesson_cancel_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "schedule:write")
    lesson = _lesson_service().get_scoped(pk=pk, user=request.user, roles=get_user_roles(request))
    if lesson is None:
        raise NotFoundException(code="not_found")
    # A branch-scoped writer (HEAD_OF_DEPT / REGISTRAR) may only cancel a lesson in their own
    # branch: scoped_lessons returns EVERY lesson for STAFF_ROLES, so without this a bare-pk
    # fetch is a cross-branch write + a cancellation-notification blast to another branch's
    # cohort. Mirrors rule_detail_view / rule_bulk_reschedule_view. cohort is select_related.
    assert_branch_id_in_scope(request, lesson.cohort.branch_id)
    data = read_json(request)
    raw = data.get("reason", "")
    reason = "" if raw in (None, "") else _str_value(raw, "reason", max_length=255, allow_blank=True)
    lesson = _lesson_service().cancel(lesson, reason=reason, actor=request.user)
    return success(lesson_to_dict(lesson))


@csrf_exempt
@require_auth
def lesson_move_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "schedule:write")
    lesson = _lesson_service().get_scoped(pk=pk, user=request.user, roles=get_user_roles(request))
    if lesson is None:
        raise NotFoundException(code="not_found")
    # Branch-scope the write (same as lesson_cancel_view): a move reschedules the class and
    # notifies its cohort, so a branch-scoped writer must not drive it on another branch's lesson.
    assert_branch_id_in_scope(request, lesson.cohort.branch_id)
    data = read_json(request)
    starts_at = _datetime_value(_require(data, "starts_at"), "starts_at")
    ends_at = _datetime_value(_require(data, "ends_at"), "ends_at")
    lesson = _lesson_service().move(lesson, starts_at=starts_at, ends_at=ends_at, actor=request.user)
    return success(lesson_to_dict(lesson))


# --- iCal feed -------------------------------------------------------------


@csrf_exempt
@require_auth
def ical_url_view(request: HttpRequest) -> HttpResponse:
    """GET /api/v1/schedule/ical-url/ — a signed, tenant-bound personal feed URL."""
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    token = _lesson_service().ical_token_for(request.user)
    url = request.build_absolute_uri(f"/api/v1/schedule/ical/{token}/")
    return success({"url": url})


@csrf_exempt
def ical_feed_view(request: HttpRequest, token: str) -> HttpResponse:
    """GET /api/v1/schedule/ical/<token>/ — PUBLIC, token-authed, text/calendar.

    No @require_auth: the signed token IS the credential. A bad/expired/cross-tenant
    token raises AuthenticationException -> the middleware returns a 401 envelope.
    """
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    service = _lesson_service()
    lessons = service.lessons_for_token(token)
    return HttpResponse(service.build_ical(lessons), content_type="text/calendar")
