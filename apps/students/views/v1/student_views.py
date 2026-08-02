"""Student endpoints — plain Django views over the layered architecture.

Two scoping layers, mirroring the old TenantSafeModelViewSet(object_scope="branch")
+ get_queryset=scoped_students:
  * ROLE scope (scoped_students): director sees all; other staff follow active
    branch/department memberships; parent/student remain children/self scoped.
  * BRANCH scope (object_scope): a branch-scoped role can only reach/mutate a
    student in its own branches (403 out_of_scope) and can only create there.
Medical notes and emergency contacts are encrypted and served only on the
detail/update payload when the caller's exact ``safeguarding:read`` membership
covers that student. See ``presenters.can_see_safeguarding_data``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.students.dto.student_dto import (
    LeadershipProfileAccessDTO,
    LeadershipProfileWindowDTO,
    StudentCreateDTO,
    TransitionDTO,
)
from apps.students.interfaces.student_service import IEnrollmentReasonService, IStudentService
from apps.students.models import StudentProfile
from apps.students.openapi_contracts import (
    LEADERSHIP_PROFILE_GET_CONTRACT,
    LEADERSHIP_PROFILE_HEAD_CONTRACT,
)
from apps.students.presenters import (
    can_see_safeguarding_data,
    enrollment_event_to_dict,
    enrollment_reason_to_dict,
    student_detail_to_dict,
    student_list_to_dict,
    student_to_dict,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.openapi_contracts import openapi_contract
from core.permissions import get_user_roles
from core.ratelimit import check_rate
from core.responses import created, error, no_content, paginated, success
from core.scoping import (
    assert_permission_membership_scope,
    permission_membership_scopes,
    request_permission_membership_allows,
)
from core.utils import current_schema

_RESOURCE = "students"
_SEARCH = ("first_name", "last_name", "phone", "student_id")
_ORDERING = ("created_at", "enrollment_date", "student_id")
_CREATE_FIELDS = frozenset(
    {
        "branch",
        "username",
        "phone",
        "email",
        "first_name",
        "last_name",
        "middle_name",
        "birthdate",
        "gender",
        "status",
        "academic_level",
        "location",
        "previous_school",
        "medical_notes",
        "emergency_contacts",
    }
)
_UPDATE_FIELDS = frozenset(
    {
        "first_name",
        "last_name",
        "middle_name",
        "phone",
        "email",
        "birthdate",
        "gender",
        "academic_level",
        "location",
        "previous_school",
        "medical_notes",
        "emergency_contacts",
    }
)


def _service() -> IStudentService:
    return container.resolve(IStudentService)  # type: ignore[type-abstract]


def _reason_service() -> IEnrollmentReasonService:
    return container.resolve(IEnrollmentReasonService)  # type: ignore[type-abstract]


def _get_in_scope(
    request: HttpRequest,
    pk: int,
    *,
    permission: str,
    lock: bool = False,
) -> StudentProfile:
    """Role-scoped fetch (404 if not visible) then branch-scope assert (403)."""
    roles = get_user_roles(request)
    student = _service().get(user=request.user, roles=roles, pk=pk)
    if student is None:
        raise NotFoundException(code="not_found")  # role-scoped out -> 404, no leak
    if lock:
        # The scoped selector contains DISTINCT relationship joins, which cannot
        # be combined safely with FOR UPDATE on PostgreSQL. Lock the one proven
        # candidate, reload its current branch/cohort, then repeat the exact
        # permission/relationship check below against the locked state.
        student = (
            StudentProfile.objects.select_for_update(of=("self",))
            .select_related("user", "branch", "current_cohort")
            .defer("medical_notes", "emergency_contacts")
            .filter(pk=student.pk)
            .first()
        )
        if student is None:
            raise NotFoundException(code="not_found")
    cohort = student.current_cohort
    department_id = cohort.department_id if cohort is not None else None
    if request_permission_membership_allows(
        request,
        permission=permission,
        branch_id=student.branch_id,
        department_id=department_id,
        account_kinds={"staff", "teacher"},
    ):
        return student
    if permission == f"{_RESOURCE}:read":
        if student.user_id == request.user.id and permission_membership_scopes(
            roles=roles,
            permission=permission,
            account_kinds={"student"},
        ):
            return student
        if permission_membership_scopes(
            roles=roles,
            permission=permission,
            account_kinds={"parent"},
        ):
            from apps.parents.models import Guardian

            user_id = request.user.pk
            if (
                user_id is not None
                and Guardian.objects.filter(
                    student=student,
                    parent__user_id=user_id,
                    revoked_at__isnull=True,
                ).exists()
            ):
                return student
    raise PermissionException(code="out_of_scope")


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
    if not read:
        return _student_detail_write(request, pk)
    check_perm(request, f"{_RESOURCE}:read")
    student = _get_in_scope(
        request,
        pk,
        permission=f"{_RESOURCE}:read",
    )
    safeguarding = can_see_safeguarding_data(request, student)
    if safeguarding:
        # Scoped selectors defer both encrypted fields. Decrypt them together
        # only after the exact safeguarding grant and method checks pass.
        student.refresh_from_db(fields=("medical_notes", "emergency_contacts"))
    return success(student_detail_to_dict(student, safeguarding=safeguarding))


def _leadership_window(request: HttpRequest) -> LeadershipProfileWindowDTO:
    allowed = {"date_from", "date_to"}
    unknown = sorted(set(request.GET) - allowed)
    duplicates = sorted(name for name in request.GET if len(request.GET.getlist(name)) != 1)
    if unknown or duplicates:
        fields = {
            name: ["Unknown query parameter." if name in unknown else "Supply this query parameter once."]
            for name in sorted(set(unknown) | set(duplicates))
        }
        raise ValidationException(
            _("Invalid leadership-profile query."),
            code="validation_error",
            fields=fields,
        )

    today = timezone.localdate()

    def value(name: str, default):
        raw = request.GET.get(name)
        if raw is None:
            return default
        parsed = parse_date(raw) if raw else None
        if parsed is None:
            raise ValidationException(
                _("Invalid leadership-profile date."),
                code="validation_error",
                fields={name: [_('Use a date in "YYYY-MM-DD" form.')]},
            )
        return parsed

    date_to = value("date_to", today)
    date_from = value("date_from", date_to - timedelta(days=89))
    if date_from > date_to:
        raise ValidationException(
            _("Invalid leadership-profile date range."),
            code="validation_error",
            fields={"date_from": [_('Must be on or before "date_to".')]},
        )
    if (date_to - date_from).days > 365:
        raise ValidationException(
            _("Leadership-profile date range is too large."),
            code="validation_error",
            fields={"date_to": [_("Choose an inclusive window of no more than 366 days.")]},
        )
    return LeadershipProfileWindowDTO(date_from=date_from, date_to=date_to)


def _leadership_access(request: HttpRequest, student: StudentProfile) -> LeadershipProfileAccessDTO:
    cohort = student.current_cohort if student.current_cohort_id else None
    department_id = cohort.department_id if cohort is not None else None

    def allows(permission: str, *, account_kinds: set[str]) -> bool:
        return request_permission_membership_allows(
            request,
            permission=permission,
            branch_id=student.branch_id,
            department_id=department_id,
            account_kinds=account_kinds,
        )

    return LeadershipProfileAccessDTO(
        academics=allows(
            "academics:read",
            account_kinds={"staff", "teacher", "parent", "student"},
        ),
        assignments=allows(
            "assignments:read",
            account_kinds={"staff", "teacher", "student"},
        ),
        attendance=allows(
            "attendance:read",
            account_kinds={"staff", "teacher", "parent", "student"},
        ),
        teachers=allows("teachers:read", account_kinds={"staff", "teacher"}),
        family=allows("parents:read", account_kinds={"staff", "parent"}),
        safeguarding=can_see_safeguarding_data(request, student),
        finance=allows("finance:read", account_kinds={"staff"}),
    )


@openapi_contract(
    path="/api/v1/students/{pk}/leadership-profile/",
    operations=(LEADERSHIP_PROFILE_GET_CONTRACT, LEADERSHIP_PROFILE_HEAD_CONTRACT),
)
@csrf_exempt
@require_auth
def student_leadership_profile_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    student = _get_in_scope(request, pk, permission=f"{_RESOURCE}:read")
    return success(
        _service().leadership_profile(
            student=student,
            user=request.user,
            roles=get_user_roles(request),
            window=_leadership_window(request),
            access=_leadership_access(request, student),
        )
    )


@transaction.atomic
def _student_detail_write(request: HttpRequest, pk: int) -> HttpResponse:
    check_perm(request, f"{_RESOURCE}:write")
    student = _get_in_scope(
        request,
        pk,
        permission=f"{_RESOURCE}:write",
        lock=True,
    )
    safeguarding = can_see_safeguarding_data(request, student)
    if request.method in ("PUT", "PATCH"):
        # Writes accept medical_notes but the echo stays role-gated (a non-medical
        # writer never receives the decrypted PHI back — DoD #4).
        body = read_json(request)
        _reject_unknown_fields(body, allowed=_UPDATE_FIELDS, operation=_("student update"))
        changes = _changes(body)
        if {"medical_notes", "emergency_contacts"} & changes.keys():
            check_perm(request, "safeguarding:write")
            cohort = student.current_cohort
            assert_permission_membership_scope(
                request,
                permission="safeguarding:write",
                branch_id=student.branch_id,
                department_id=cohort.department_id if cohort is not None else None,
                account_kinds={"staff"},
            )
        updated = _service().update(student, changes)
        if safeguarding:
            updated.refresh_from_db(fields=("medical_notes", "emergency_contacts"))
        return success(student_detail_to_dict(updated, safeguarding=safeguarding))
    if request.method == "DELETE":
        _reject_unknown_fields(read_json(request), allowed=frozenset(), operation=_("student deactivation"))
        _service().deactivate(student, actor=request.user)
        return no_content()
    return error("Method not allowed.", code="method_not_allowed", status=405)


# --- detail actions --------------------------------------------------------
@csrf_exempt
@require_auth
@transaction.atomic
def student_transition_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    student = _get_in_scope(request, pk, permission=f"{_RESOURCE}:write", lock=True)
    body = read_json(request)
    _reject_unknown_fields(
        body,
        allowed=frozenset({"to_status", "reason_code", "note"}),
        operation=_("student transition"),
    )
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
@transaction.atomic
def student_block_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    student = _get_in_scope(request, pk, permission=f"{_RESOURCE}:write", lock=True)
    body = read_json(request)
    _reject_unknown_fields(body, allowed=frozenset({"reason"}), operation=_("student block"))
    reason = str_field(body, "reason", max_length=255)
    return success(student_to_dict(_service().block(student, reason, actor=request.user)))


@csrf_exempt
@require_auth
@transaction.atomic
def student_unblock_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _reject_unknown_fields(read_json(request), allowed=frozenset(), operation=_("student unblock"))
    student = _get_in_scope(request, pk, permission=f"{_RESOURCE}:write", lock=True)
    return success(student_to_dict(_service().unblock(student, actor=request.user)))


@csrf_exempt
@require_auth
def student_events_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    student = _get_in_scope(request, pk, permission=f"{_RESOURCE}:read")
    return success([enrollment_event_to_dict(e) for e in _service().events(student)])


@csrf_exempt
@require_auth
@transaction.atomic
def student_credentials_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Issue a ONE-TIME login password for the student so they can sign in at /role-login/
    (accounts are created passwordless). Returns {username, temporary_password}; the student
    is flagged to change it on first login. students:write + role/branch scope; writes under
    a read-only impersonation token are already blocked by check_perm."""
    if request.method != "POST":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:write")
    _reject_unknown_fields(read_json(request), allowed=frozenset(), operation=_("student credentials"))
    student = _get_in_scope(request, pk, permission=f"{_RESOURCE}:write", lock=True)
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
    _reject_unknown_fields(
        body,
        allowed=frozenset({"name", "slug", "color", "is_active"}),
        operation=_("enrollment-reason creation"),
    )
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
    _reject_unknown_fields(
        body,
        allowed=frozenset({"name", "slug", "color", "is_active"}),
        operation=_("enrollment-reason update"),
    )
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
        _reject_unknown_fields(
            read_json(request),
            allowed=frozenset(),
            operation=_("enrollment-reason deactivation"),
        )
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
    assert_permission_membership_scope(
        request,
        permission=f"{_RESOURCE}:write",
        branch_id=branch_id,
        enforce_department=False,
        account_kinds={"staff", "teacher"},
    )
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
    return paginated(
        [student_list_to_dict(s) for s in items],
        total=total,
        page=page,
        page_size=size,
    )


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
    cleaned = fs.form.cleaned_data
    range_errors: dict[str, list[str]] = {}
    joined_after, joined_before = cleaned.get("joined_after"), cleaned.get("joined_before")
    if joined_after and joined_before and joined_after > joined_before:
        message = "Must be on or before joined_before."
        range_errors["joined_after"] = [message]
        range_errors["joined_before"] = ["Must be on or after joined_after."]
    age_min, age_max = cleaned.get("age_min"), cleaned.get("age_max")
    for field, value in (("age_min", age_min), ("age_max", age_max)):
        if value is not None and (value != int(value) or not 0 <= value <= 120):
            range_errors[field] = ["Enter a whole age between 0 and 120."]
    if age_min is not None and age_max is not None and age_min > age_max:
        range_errors["age_min"] = ["Must be less than or equal to age_max."]
        range_errors["age_max"] = ["Must be greater than or equal to age_min."]
    if range_errors:
        raise ValidationException(
            "Invalid filter range.",
            code="validation_error",
            fields=range_errors,
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
    return paginated(
        [student_list_to_dict(s) for s in items],
        total=total,
        page=page,
        page_size=size,
    )


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
    _reject_unknown_fields(body, allowed=_CREATE_FIELDS, operation=_("student creation"))
    phone, email = str_field(body, "phone", max_length=32), str_field(body, "email", max_length=254)
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
    assert_permission_membership_scope(
        request,
        permission=f"{_RESOURCE}:write",
        branch_id=branch_id,
        enforce_department=False,
        account_kinds={"staff", "teacher"},
    )
    if "medical_notes" in body or "emergency_contacts" in body:
        check_perm(request, "safeguarding:write")
        # A new student has no department-bearing cohort yet.  Consequently a
        # department-only safeguarding grant cannot safely authorize this
        # write; only a branch-wide or organization-wide membership can.
        assert_permission_membership_scope(
            request,
            permission="safeguarding:write",
            branch_id=branch_id,
            department_id=None,
            account_kinds={"staff"},
        )
    dto = StudentCreateDTO(
        branch_id=branch_id,  # type: ignore[arg-type]
        username=str_field(body, "username", max_length=150),
        phone=phone,
        email=email,
        first_name=str_field(body, "first_name", max_length=150),
        last_name=str_field(body, "last_name", max_length=150),
        middle_name=str_field(body, "middle_name", max_length=150),
        birthdate=_date_or_none(body, "birthdate"),
        gender=_choice(body, "gender", StudentProfile.Gender.values, allow_blank=True),
        status=_choice(body, "status", StudentProfile.Status.values, default=StudentProfile.Status.LEAD),
        academic_level=str_field(body, "academic_level", max_length=64),
        location=str_field(body, "location", max_length=200),
        previous_school=str_field(body, "previous_school", max_length=200),
        medical_notes=str_field(body, "medical_notes"),
        emergency_contacts=_emergency_contacts(body),
    )
    return created(student_to_dict(_service().create(dto)))  # ReadSerializer -> no medical_notes echoed


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    """Only the directly-editable fields (StudentUpdateSerializer): current_cohort,
    branch, and status are intentionally NOT writable here."""
    changes: dict[str, Any] = {}
    identity_lengths = {
        "first_name": 150,
        "last_name": 150,
        "middle_name": 150,
        "phone": 32,
        "email": 254,
    }
    for f, max_length in identity_lengths.items():
        if f in body:
            if body[f] is None:
                raise ValidationException(
                    "Invalid input.", code="validation_error", fields={f: ["May not be null."]}
                )
            changes[f] = str_field(body, f, max_length=max_length)
    if "birthdate" in body:
        changes["birthdate"] = _date_or_none(body, "birthdate")
    if "gender" in body:
        changes["gender"] = _choice(body, "gender", StudentProfile.Gender.values, allow_blank=True)
    scalar_lengths: dict[str, int | None] = {
        "academic_level": 64,
        "location": 200,
        "previous_school": 200,
        "medical_notes": None,
    }
    for f, scalar_max_length in scalar_lengths.items():
        if f in body:
            if body[f] is None:
                raise ValidationException(
                    "Invalid input.", code="validation_error", fields={f: ["May not be null."]}
                )
            changes[f] = str_field(body, f, max_length=scalar_max_length)
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


def _reject_unknown_fields(
    body: dict[str, Any],
    *,
    allowed: frozenset[str],
    operation,
) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationException(
            _("Unsupported field in %(operation)s.") % {"operation": operation},
            code="validation_error",
            fields={field: [_("This field is not supported.")] for field in unknown},
        )
