"""Student endpoints — plain Django views over the layered architecture.

Two scoping layers, mirroring the old TenantSafeModelViewSet(object_scope="branch")
+ get_queryset=scoped_students:
  * ROLE scope (scoped_students): staff see all, a parent sees their children, a
    student sees only self — applied to the list and as the 404 gate on detail.
  * BRANCH scope (object_scope): a branch-scoped role can only reach/mutate a
    student in its own branches (403 out_of_scope) and can only create there.
medical_notes (encrypted PHI) is served ONLY on the detail/update payload and ONLY
to a DIRECTOR/REGISTRAR (or superuser) — see presenters.can_see_medical_notes.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.students.dto.student_dto import StudentCreateDTO, TransitionDTO
from apps.students.interfaces.student_service import IEnrollmentReasonService, IStudentService
from apps.students.models import StudentProfile
from apps.students.presenters import (
    can_see_medical_notes,
    enrollment_event_to_dict,
    enrollment_reason_to_dict,
    student_detail_to_dict,
    student_to_dict,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles
from core.ratelimit import check_rate
from core.responses import created, error, no_content, paginated, success
from core.scoping import assert_branch_id_in_scope, assert_in_branch_scope
from core.utils import current_schema

_RESOURCE = "students"
_SEARCH = ("first_name", "last_name", "phone", "student_id")
_ORDERING = ("created_at", "enrollment_date", "student_id")


def _service() -> IStudentService:
    return container.resolve(IStudentService)  # type: ignore[type-abstract]


def _reason_service() -> IEnrollmentReasonService:
    return container.resolve(IEnrollmentReasonService)  # type: ignore[type-abstract]


def _get_in_scope(request: HttpRequest, pk: int) -> StudentProfile:
    """Role-scoped fetch (404 if not visible) then branch-scope assert (403)."""
    student = _service().get(user=request.user, roles=get_user_roles(request), pk=pk)
    if student is None:
        raise NotFoundException(code="not_found")  # role-scoped out -> 404, no leak
    assert_in_branch_scope(request, student)  # object_scope="branch" -> 403 out_of_scope
    return student


# --- collection: GET list / POST create -----------------------------------
@csrf_exempt
@require_auth
def students_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        return _list(request)
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        return _create(request)
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def student_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    student = _get_in_scope(request, pk)
    medical = can_see_medical_notes(request)
    if read:
        return success(student_detail_to_dict(student, medical=medical))
    if request.method in ("PUT", "PATCH"):
        # Writes accept medical_notes but the echo stays role-gated (a non-medical
        # writer never receives the decrypted PHI back — DoD #4).
        updated = _service().update(student, _changes(read_json(request)))
        return success(student_detail_to_dict(updated, medical=medical))
    if request.method == "DELETE":
        _service().delete(student)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


# --- detail actions --------------------------------------------------------
@csrf_exempt
@require_auth
def student_transition_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    student = _get_in_scope(request, pk)
    body = read_json(request)
    dto = TransitionDTO(
        to_status=_choice(body, "to_status", StudentProfile.Status.values, required=True),
        # Validated against the center's active, configurable EnrollmentReason slugs
        # (was the hardcoded ReasonCode enum). Blank stays allowed.
        reason_code=_choice(body, "reason_code", _reason_service().active_slugs(), allow_blank=True),
        note=str_field(body, "note"),
    )
    return success(student_to_dict(_service().transition(student, dto, actor=request.user)))


@csrf_exempt
@require_auth
def student_block_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    student = _get_in_scope(request, pk)
    reason = str_field(read_json(request), "reason")
    return success(student_to_dict(_service().block(student, reason, actor=request.user)))


@csrf_exempt
@require_auth
def student_unblock_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    student = _get_in_scope(request, pk)
    return success(student_to_dict(_service().unblock(student, actor=request.user)))


@csrf_exempt
@require_auth
def student_events_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    student = _get_in_scope(request, pk)
    return success([enrollment_event_to_dict(e) for e in _service().events(student)])


@csrf_exempt
@require_auth
def student_credentials_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Issue a ONE-TIME login password for the student so they can sign in at /role-login/
    (accounts are created passwordless). Returns {username, temporary_password}; the student
    is flagged to change it on first login. students:write + role/branch scope; writes under
    a read-only impersonation token are already blocked by check_perm."""
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    student = _get_in_scope(request, pk)
    return success(_service().issue_credentials(student, actor=request.user))


# --- enrollment reasons (per-Center configurable) --------------------------
def _reason_reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _reason_slug(body: dict[str, Any], *, required: bool) -> str | None:
    import re

    raw = body.get("slug")
    if raw in (None, ""):
        if required:
            raise _reason_reject("slug", "This field is required.")
        return None
    value = str_field(body, "slug", max_length=64)
    if not re.fullmatch(r"[-a-zA-Z0-9_]+", value):
        raise _reason_reject("slug", "Enter a valid slug (letters, numbers, hyphens, underscores).")
    return value


def _reason_create_data(body: dict[str, Any]) -> dict[str, Any]:
    name = str_field(body, "name", max_length=64)
    if not name:
        raise _reason_reject("name", "This field is required.")
    out: dict[str, Any] = {
        "name": name,
        "color": str_field(body, "color", max_length=16),
        "is_active": bool_field(body, "is_active", default=True),
    }
    slug = _reason_slug(body, required=False)
    if slug:
        out["slug"] = slug
    return out


def _reason_changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "name" in body:
        name = str_field(body, "name", max_length=64)
        if not name:
            raise _reason_reject("name", "This field may not be blank.")
        changes["name"] = name
    if "slug" in body:
        changes["slug"] = _reason_slug(body, required=True)
    if "color" in body:
        changes["color"] = str_field(body, "color", max_length=16)
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active")
    return changes


@csrf_exempt
@require_auth
def enrollment_reasons_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:read")
        qs = apply_filters(
            request,
            _reason_service().list_reasons(),
            filter_fields=("is_active",),
            search_fields=("name", "slug"),
            ordering_fields=("name",),
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated(
            [enrollment_reason_to_dict(r) for r in items], total=total, page=page, page_size=size
        )
    if request.method == "POST":
        check_perm(request, f"{_RESOURCE}:write")
        reason = _reason_service().create(data=_reason_create_data(read_json(request)))
        return created(enrollment_reason_to_dict(reason))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def enrollment_reason_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, f"{_RESOURCE}:read" if read else f"{_RESOURCE}:write")
    reason = _reason_service().get(pk=pk)
    if reason is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(enrollment_reason_to_dict(reason))
    if request.method in ("PUT", "PATCH"):
        updated = _reason_service().update(reason, changes=_reason_changes(read_json(request)))
        return success(enrollment_reason_to_dict(updated))
    if request.method == "DELETE":
        _reason_service().delete(reason)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


# --- collection actions ----------------------------------------------------
@csrf_exempt
@require_auth
def students_import_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    # bulk_import throttle: 6/min per (schema, user) — mirrors BulkImportThrottle.
    check_rate(scope="bulk_import", key=f"{current_schema()}:{request.user.pk}", limit=6, window=60)
    file_obj = request.FILES.get("file")
    if file_obj is None:
        raise ValidationException(
            "File is required.", code="validation_error", fields={"file": ["This field is required."]}
        )
    branch_id = int_field(request.POST, "branch", required=True)
    # Same create-scope as the single-student POST: a branch-scoped role must not
    # mass-create students into a branch outside its memberships.
    assert_branch_id_in_scope(request, branch_id)
    result = _service().import_csv(file_obj=file_obj, branch_id=branch_id)  # type: ignore[arg-type]
    return created(result)


@csrf_exempt
@require_auth
def students_birthdays_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    days = int_field(request.GET, "days", default=7)
    if days is None or not (0 <= days <= 366):
        raise ValidationException(
            "days must be between 0 and 366.",
            code="validation_error",
            fields={"days": ["Must be between 0 and 366."]},
        )
    qs = _service().birthdays(
        user=request.user,
        roles=get_user_roles(request),
        days=days,
        branch=int_field(request.GET, "branch"),
        cohort=int_field(request.GET, "cohort"),
    )
    items, total, page, size = paginate(request, qs)
    return paginated([student_to_dict(s) for s in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def students_stats_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    return success(_service().stats(user=request.user, roles=get_user_roles(request)))


@csrf_exempt
@require_auth
def students_comparison_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    metric = _choice(request.GET, "metric", ("joined", "left"), default="joined")
    unit = _choice(request.GET, "unit", ("hour", "day", "week", "month", "year"), default="month")
    return success(
        _service().comparison(user=request.user, roles=get_user_roles(request), metric=metric, unit=unit)
    )


# --- self-service (authenticated-only; own profile) ------------------------
@csrf_exempt
@require_auth
def student_dashboard_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return success(_service().dashboard(user=request.user, roles=get_user_roles(request)))


@csrf_exempt
@require_auth
def student_report_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    return success(_service().report(user=request.user))


# --- helpers ---------------------------------------------------------------
def _list(request: HttpRequest) -> HttpResponse:
    from apps.students.filters import StudentFilter

    qs = _service().scoped_list(user=request.user, roles=get_user_roles(request))
    fs = StudentFilter(request.GET, queryset=qs)
    if not fs.is_valid():
        raise ValidationException(
            "Invalid filter parameters.",
            code="validation_error",
            fields={k: [str(e) for e in v] for k, v in fs.errors.items()},
        )
    qs = apply_filters(
        request,
        fs.qs,
        filter_fields=(),  # StudentFilter already applied the rich filtering
        search_fields=_SEARCH,
        ordering_fields=_ORDERING,
        default_ordering="-created_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([student_to_dict(s) for s in items], total=total, page=page, page_size=size)


def _date_or_none(data: dict[str, Any], name: str):
    """Parse an optional YYYY-MM-DD date: None when absent/blank; 400 on a bad value."""
    raw = data.get(name)
    if raw in (None, ""):
        return None
    parsed = None
    if isinstance(raw, str):
        from django.utils.dateparse import parse_date

        try:
            parsed = parse_date(raw)
        except ValueError:
            parsed = None
    if parsed is None:
        raise ValidationException(
            f"Invalid {name}.",
            code="validation_error",
            fields={name: ["Enter a valid date (YYYY-MM-DD)."]},
        )
    return parsed


def _create(request: HttpRequest) -> HttpResponse:
    body = read_json(request)
    phone, email = str_field(body, "phone"), str_field(body, "email")
    if not phone and not email:
        raise ValidationException(
            "Provide a phone or an email.",
            code="validation_error",
            fields={"phone": ["Provide a phone or an email."]},
        )
    branch_id = int_field(body, "branch", required=True)
    # Validate the branch is active (400) BEFORE the create-scope check (403), matching
    # the old serializer (PrimaryKeyRelatedField over active branches) running before
    # perform_create's scope assertion.
    _assert_active_branch(branch_id)
    assert_branch_id_in_scope(request, branch_id)  # create-scope (object_scope="branch")
    dto = StudentCreateDTO(
        branch_id=branch_id,  # type: ignore[arg-type]
        username=str_field(body, "username"),
        phone=phone,
        email=email,
        first_name=str_field(body, "first_name"),
        last_name=str_field(body, "last_name"),
        middle_name=str_field(body, "middle_name"),
        birthdate=_date_or_none(body, "birthdate"),
        gender=_choice(body, "gender", StudentProfile.Gender.values, allow_blank=True),
        status=_choice(body, "status", StudentProfile.Status.values, default=StudentProfile.Status.LEAD),
        academic_level=str_field(body, "academic_level"),
        location=str_field(body, "location"),
        previous_school=str_field(body, "previous_school"),
        medical_notes=str_field(body, "medical_notes"),
        emergency_contacts=_emergency_contacts(body),
    )
    return created(student_to_dict(_service().create(dto)))  # ReadSerializer -> no medical_notes echoed


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    """Only the directly-editable fields (StudentUpdateSerializer): current_cohort,
    branch, and status are intentionally NOT writable here."""
    changes: dict[str, Any] = {}
    for f in ("first_name", "last_name", "middle_name", "phone", "email"):
        if f in body:
            changes[f] = str_field(body, f)
    if "birthdate" in body:
        changes["birthdate"] = _date_or_none(body, "birthdate")
    if "gender" in body:
        changes["gender"] = _choice(body, "gender", StudentProfile.Gender.values, allow_blank=True)
    if "is_active" in body:
        changes["is_active"] = bool_field(body, "is_active")
    for f in ("academic_level", "location", "previous_school", "medical_notes"):
        if f in body:
            changes[f] = str_field(body, f)
    if "emergency_contacts" in body:
        changes["emergency_contacts"] = _emergency_contacts(body)
    return changes


def _assert_active_branch(branch_id: int | None) -> None:
    """400 invalid_branch if the branch is missing or archived (not assignable)."""
    from apps.org.models import Branch

    if branch_id is None or not Branch.objects.filter(pk=branch_id, archived_at__isnull=True).exists():
        raise ValidationException("Invalid branch.", code="invalid_branch", fields={"branch": ["Not found."]})


def _emergency_contacts(body: dict[str, Any]) -> list:
    raw = body.get("emergency_contacts", [])
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValidationException(
            "Invalid emergency_contacts.",
            code="validation_error",
            fields={"emergency_contacts": ["Must be a list."]},
        )
    return raw


def _choice(
    data: dict[str, Any],
    name: str,
    choices,
    *,
    required: bool = False,
    allow_blank: bool = False,
    default: str = "",
) -> str:
    raw = data.get(name)
    if raw in (None, ""):
        if required:
            raise ValidationException(
                f"{name} is required.", code="validation_error", fields={name: ["This field is required."]}
            )
        return "" if allow_blank and raw == "" else default
    value = str(raw)
    if value not in choices:
        raise ValidationException(
            f"Invalid {name}.", code="validation_error", fields={name: ["Not a valid choice."]}
        )
    return value
