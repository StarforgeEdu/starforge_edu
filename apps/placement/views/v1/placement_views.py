"""Placement HTTP views (layered, off DRF).

The test bank (build/approve lifecycle, maker-checker), the sitting + auto-grading
attempts (with the ANSWER-KEY GATE: a lead never sees is_correct or the key), and
group placement. The heavy logic lives in the preserved apps.placement.services
domain functions behind IPlacementService; the nuanced role/branch scoping
(reproducing the old get_queryset) is done here in the views.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from apps.academics.models import Subject
from apps.cohorts.models import Cohort
from apps.org.models import Branch
from apps.placement.interfaces.services import IPlacementService
from apps.placement.models import PlacementAttempt, PlacementTest
from apps.placement.presenters import (
    group_proposal_to_dict,
    placement_attempt_to_dict,
    placement_question_to_dict,
    placement_test_to_dict,
)
from apps.students.models import StudentProfile
from apps.students.selectors import student_profile_for
from core.api_auth import check_perm, deny_read_only_token, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import read_json, str_field
from core.listing import apply_filters, paginate
from core.permissions import Role, get_role_memberships, get_user_roles, has_permission_code
from core.responses import created, error, no_content, paginated, success

_DIFFICULTIES = frozenset({"easy", "medium", "hard"})


def _service() -> IPlacementService:
    return container.resolve(IPlacementService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _assert_test_creation_client(request: HttpRequest) -> None:
    """F8-2: when the center restricts placement-test authoring to the mobile app, a
    request that does not identify as the mobile client (``X-Client: mobile``) is 403'd.
    A soft, spoofable steering gate (a determined web caller can send the header) — the
    intent is to route staff to the mobile authoring tools, not to be a security boundary."""
    from apps.org.selectors import get_center_settings

    if not get_center_settings().placement_test_creation_mobile_only:
        return
    if request.META.get("HTTP_X_CLIENT", "").strip().lower() != "mobile":
        raise PermissionException(
            _("Placement tests can only be authored from the mobile app."),
            code="web_test_creation_blocked",
        )


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require(data: dict[str, Any], name: str) -> Any:
    if name not in data or data[name] is None:
        raise _reject(name, "This field is required.")
    return data[name]


def _str_required(raw: Any, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    if "\x00" in raw:
        raise _reject(name, "Null characters are not allowed.")
    value = raw.strip()
    if not value:
        raise _reject(name, "This field may not be blank.")
    if max_length is not None and len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _str_value(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    return raw


def _int_bounded(raw: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _reject(name, "Must be an integer.")
    if raw < lo or raw > hi:
        raise _reject(name, f"Must be between {lo} and {hi}.")
    return raw


def _int_id(raw: Any, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _reject(name, "Must be an integer id.")
    return raw


def _choice(raw: Any, name: str, choices: frozenset[str]) -> str:
    # isinstance guard BEFORE the frozenset test (a list/dict would raise an
    # unhashable-type TypeError -> 500 instead of a clean 400).
    if not isinstance(raw, str) or raw not in choices:
        raise _reject(name, f"Must be one of: {', '.join(sorted(choices))}.")
    return raw


def _reason(request: HttpRequest) -> str:
    return _str_required(_require(read_json(request), "reason"), "reason", max_length=255)


# --- request-scoping helpers (reproduce the old get_queryset rules) --------


def _roles(request: HttpRequest) -> set[str]:
    return get_user_roles(request)


def _branch_ids(request: HttpRequest) -> set[int]:
    return {m.branch_id for m in get_role_memberships(request) if m.branch_id}


def _is_director(request: HttpRequest) -> bool:
    user: Any = request.user
    return bool(user.is_superuser) or Role.DIRECTOR in _roles(request)


def _actor_is_staff(request: HttpRequest) -> bool:
    return _is_director(request) or has_permission_code(_roles(request), "placement:write")


def _scoped_tests(request: HttpRequest) -> QuerySet[PlacementTest]:
    base = _service().tests_base()
    if _is_director(request):
        return base
    if has_permission_code(_roles(request), "placement:write"):
        return base.filter(Q(created_by=request.user) | Q(branch_id__in=_branch_ids(request)))
    return base.none()


def _scoped_attempts(request: HttpRequest) -> QuerySet[PlacementAttempt]:
    base = _service().attempts_base()
    if _is_director(request):
        return base
    user: Any = request.user
    if has_permission_code(_roles(request), "placement:write"):
        return base.filter(
            Q(assigned_by=user) | Q(test__branch_id__in=_branch_ids(request))
        ).distinct()
    profile = student_profile_for(user)
    if profile is not None:
        return base.filter(student=profile)
    return base.none()


def _scoped_proposals(request: HttpRequest) -> QuerySet:
    base = _service().proposals_base()
    if _is_director(request):
        return base
    return base.filter(
        Q(proposed_by=request.user) | Q(cohort__branch_id__in=_branch_ids(request))
    ).distinct()


def _get_test(request: HttpRequest, pk: int) -> PlacementTest:
    obj = _scoped_tests(request).filter(pk=pk).first()
    if obj is None:
        raise NotFoundException(code="not_found")
    return obj


def _get_attempt(request: HttpRequest, pk: int) -> PlacementAttempt:
    obj = _scoped_attempts(request).filter(pk=pk).first()
    if obj is None:
        raise NotFoundException(code="not_found")
    return obj


def _get_proposal(request: HttpRequest, pk: int):
    obj = _scoped_proposals(request).filter(pk=pk).first()
    if obj is None:
        raise NotFoundException(code="not_found")
    return obj


# --- FK resolution ---------------------------------------------------------


def _resolve_subject(data: dict[str, Any]):
    if "subject" not in data or data["subject"] is None:
        return None
    obj = Subject.objects.filter(pk=_int_id(data["subject"], "subject")).first()
    if obj is None:
        raise _reject("subject", "Invalid subject.")
    return obj


def _resolve_branch(data: dict[str, Any]):
    if "branch" not in data or data["branch"] is None:
        return None
    obj = Branch.objects.filter(pk=_int_id(data["branch"], "branch"), archived_at__isnull=True).first()
    if obj is None:
        raise _reject("branch", "Invalid branch.")
    return obj


def _resolve_test(data: dict[str, Any]) -> PlacementTest:
    obj = PlacementTest.objects.filter(pk=_int_id(_require(data, "test"), "test")).first()
    if obj is None:
        raise _reject("test", "Invalid test.")
    return obj


def _resolve_student(data: dict[str, Any]) -> StudentProfile:
    obj = StudentProfile.objects.filter(pk=_int_id(_require(data, "student"), "student")).first()
    if obj is None:
        raise _reject("student", "Invalid student.")
    return obj


def _resolve_cohort(data: dict[str, Any]) -> Cohort:
    obj = Cohort.objects.filter(pk=_int_id(_require(data, "cohort"), "cohort")).first()
    if obj is None:
        raise _reject("cohort", "Invalid cohort.")
    return obj


def _time_limit(data: dict[str, Any]) -> int | None:
    if "time_limit_minutes" not in data or data["time_limit_minutes"] is None:
        return None
    return _int_bounded(data["time_limit_minutes"], "time_limit_minutes", 1, 600)


# --- placement tests -------------------------------------------------------


def _create_test_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    subject = _resolve_subject(data)
    branch = _resolve_branch(data)
    if not _is_director(request):
        # A non-director builds only within their own branch; only the director may
        # create a centre-wide (branch=None) placement test.
        my_branches = _branch_ids(request)
        if branch is None:
            if len(my_branches) == 1:
                branch = Branch.objects.get(pk=next(iter(my_branches)))
            else:
                raise ValidationException(_("Choose a branch for this test."), code="branch_required")
        elif branch.id not in my_branches:
            raise PermissionException(
                _("You can only create tests in your own branch."), code="cross_branch"
            )
    return {
        "title": _str_required(_require(data, "title"), "title", max_length=200),
        "description": str_field(data, "description"),
        "subject": subject,
        "branch": branch,
        "time_limit_minutes": _time_limit(data),
    }


def _update_test_changes(request: HttpRequest) -> dict[str, Any]:
    """PATCH/PUT metadata (the update serializer is all-optional, so PUT == PATCH).
    update_test only applies title/description/subject/time_limit_minutes."""
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "title" in data:
        changes["title"] = _str_required(data["title"], "title", max_length=200)
    if "description" in data:
        changes["description"] = str_field(data, "description")
    if "subject" in data:
        changes["subject"] = _resolve_subject(data)  # explicit null clears it
    if "time_limit_minutes" in data:
        changes["time_limit_minutes"] = _time_limit(data)
    return changes


@csrf_exempt
@require_auth
def tests_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "placement:read")
        qs = apply_filters(
            request,
            _scoped_tests(request),
            filter_fields=("status", "branch", "subject"),
            search_fields=("title",),
            ordering_fields=("created_at", "title"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([placement_test_to_dict(t) for t in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "placement:write")
        _assert_test_creation_client(request)
        test = _service().create_test(created_by=request.user, **_create_test_data(request))
        return created(placement_test_to_dict(test))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def test_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "placement:read")
        return success(placement_test_to_dict(_get_test(request, pk)))
    if request.method in ("PUT", "PATCH"):
        check_perm(request, "placement:write")
        test = _get_test(request, pk)
        test = _service().update_test(test=test, changes=_update_test_changes(request))
        return success(placement_test_to_dict(test))
    if request.method == "DELETE":
        check_perm(request, "placement:write")
        _service().delete_test(test=_get_test(request, pk))
        return no_content()
    return _method_not_allowed()


@csrf_exempt
@require_auth
def test_add_question_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:write")
    _assert_test_creation_client(request)
    test = _get_test(request, pk)
    data = read_json(request)
    question = _service().add_question(
        test=test,
        prompt=_str_required(_require(data, "prompt"), "prompt"),
        # question_type MUST be a str before the domain's `in _CHOICE_TYPES` set test.
        question_type=_str_value(_require(data, "question_type"), "question_type"),
        options=data.get("options", []),  # the domain validates list-ness + semantics
        correct_answer=data.get("correct_answer"),
        points=_points_value(data),
        media=data.get("media"),  # the domain validates dict-ness (F8-1 media questions)
    )
    return created(placement_question_to_dict(question))


def _points_value(data: dict[str, Any]) -> int:
    if "points" not in data or data["points"] is None:
        return 1  # PositiveSmallIntegerField default
    raw = data["points"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _reject("points", "Must be an integer.")
    if raw < 0 or raw > 32767:
        raise _reject("points", "Must be between 0 and 32767.")
    return raw


@csrf_exempt
@require_auth
def test_generate_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:write")
    _assert_test_creation_client(request)
    test = _get_test(request, pk)
    data = read_json(request)
    ai_request = _service().request_generation(
        test=test,
        requested_by=request.user,
        count=_int_bounded(_require(data, "count"), "count", 1, 50),
        difficulty=_choice(data.get("difficulty", "medium"), "difficulty", _DIFFICULTIES),
        topic=str_field(data, "topic", max_length=200),
    )
    return success({"request_id": ai_request.pk, "status": ai_request.status}, status=202)


@csrf_exempt
@require_auth
def test_remove_question_view(request: HttpRequest, pk: int, question_id: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:write")
    _assert_test_creation_client(request)
    test = _get_test(request, pk)
    question = test.questions.filter(pk=question_id).first()
    if question is None:
        raise NotFoundException(_("That question is not on this test."), code="question_not_found")
    _service().remove_question(question=question)
    return no_content()


@csrf_exempt
@require_auth
def test_submit_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:write")
    _assert_test_creation_client(request)
    test = _service().submit_test(test=_get_test(request, pk))
    return success(placement_test_to_dict(test))


@csrf_exempt
@require_auth
def test_approve_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:approve")
    test = _service().approve_test(test=_get_test(request, pk), approver=request.user)
    return success(placement_test_to_dict(test))


@csrf_exempt
@require_auth
def test_reject_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:approve")
    test = _get_test(request, pk)
    test = _service().reject_test(test=test, reviewer=request.user, reason=_reason(request))
    return success(placement_test_to_dict(test))


# --- placement attempts ----------------------------------------------------


def _assert_attempt_scope(request: HttpRequest, test: PlacementTest, student: StudentProfile) -> None:
    if _is_director(request):
        return
    my_branches = _branch_ids(request)
    if test.branch_id is not None and test.branch_id not in my_branches:
        raise PermissionException(
            _("You can only assign a test from your own branch."), code="cross_branch"
        )
    if student.branch_id not in my_branches:
        raise PermissionException(
            _("You can only assign to a student in your own branch."), code="cross_branch"
        )


def _answers(request: HttpRequest) -> list[dict]:
    answers = _require(read_json(request), "answers")
    if not isinstance(answers, list):
        raise _reject("answers", "answers must be a list of {question, response} objects.")
    for item in answers:
        if not isinstance(item, dict) or "question" not in item:
            raise _reject("answers", "each answer needs a 'question' id.")
    return answers


def _marks(request: HttpRequest) -> list[dict]:
    marks = _require(read_json(request), "marks")
    if not isinstance(marks, list) or not marks:
        raise _reject("marks", "marks must be a non-empty list of {question, score} objects.")
    for item in marks:
        if not isinstance(item, dict) or "question" not in item or "score" not in item:
            raise _reject("marks", "each mark needs a 'question' id and a 'score'.")
    return marks


@csrf_exempt
@require_auth
def attempts_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        # list is a SELF action (any authed user) — row-scoped by _scoped_attempts.
        qs = apply_filters(
            request,
            _scoped_attempts(request),
            filter_fields=("status", "test", "student"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        staff = _actor_is_staff(request)
        return paginated(
            [placement_attempt_to_dict(a, staff_view=staff) for a in items],
            total=total,
            page=page,
            page_size=size,
        )
    if request.method == "POST":
        check_perm(request, "placement:write")
        data = read_json(request)
        test = _resolve_test(data)
        student = _resolve_student(data)
        _assert_attempt_scope(request, test, student)
        attempt = _service().assign(test=test, student=student, assigned_by=request.user)
        return created(placement_attempt_to_dict(attempt, staff_view=_actor_is_staff(request)))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def attempt_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    # retrieve is a SELF action — row-scoped by _get_attempt.
    attempt = _get_attempt(request, pk)
    return success(placement_attempt_to_dict(attempt, staff_view=_actor_is_staff(request)))


@csrf_exempt
@require_auth
def attempt_submit_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    # submit is a SELF action (any authed user, row-scoped) with no perm code -> it
    # must still deny a read-only impersonation token (old TenantSafe write-deny).
    deny_read_only_token(request)
    attempt = _get_attempt(request, pk)
    attempt = _service().submit_attempt(attempt=attempt, answers=_answers(request))
    return success(placement_attempt_to_dict(attempt, staff_view=_actor_is_staff(request)))


@csrf_exempt
@require_auth
def attempt_suggestions_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "placement:write")  # reception's placing tool, not the lead's
    attempt = _get_attempt(request, pk)
    return success(_service().suggestions(student=attempt.student))


@csrf_exempt
@require_auth
def attempt_mark_writing_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:write")
    ai_request = _service().request_writing_marking(
        attempt=_get_attempt(request, pk), requested_by=request.user
    )
    return success({"request_id": ai_request.pk, "status": ai_request.status}, status=202)


@csrf_exempt
@require_auth
def attempt_mark_writing_manual_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:write")
    attempt = _service().mark_writing_manual(attempt=_get_attempt(request, pk), marks=_marks(request))
    # A staff-only action -> always the full (staff) view.
    return success(placement_attempt_to_dict(attempt, staff_view=True))


# --- group proposals -------------------------------------------------------


def _assert_proposal_scope(request: HttpRequest, cohort: Cohort) -> None:
    if _is_director(request):
        return
    if cohort.branch_id not in _branch_ids(request):
        raise PermissionException(
            _("You can only place students into your own branch's groups."), code="cross_branch"
        )


@csrf_exempt
@require_auth
def proposals_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "placement:read")
        qs = apply_filters(
            request,
            _scoped_proposals(request),
            filter_fields=("status", "student", "cohort"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([group_proposal_to_dict(p) for p in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "placement:write")
        data = read_json(request)
        student = _resolve_student(data)
        cohort = _resolve_cohort(data)
        _assert_proposal_scope(request, cohort)
        proposal = _service().propose(student=student, cohort=cohort, proposed_by=request.user)
        return created(group_proposal_to_dict(proposal))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def proposal_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "placement:read")
    return success(group_proposal_to_dict(_get_proposal(request, pk)))


@csrf_exempt
@require_auth
def proposal_accept_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:approve")
    proposal = _service().accept(proposal=_get_proposal(request, pk), manager=request.user)
    return success(group_proposal_to_dict(proposal))


@csrf_exempt
@require_auth
def proposal_reject_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "placement:approve")
    proposal = _get_proposal(request, pk)
    proposal = _service().reject_proposal(proposal=proposal, manager=request.user, reason=_reason(request))
    return success(group_proposal_to_dict(proposal))
