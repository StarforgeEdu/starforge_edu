"""Tenant-scoped upload, review, and confirmation endpoints."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.people_imports.models import PeopleImportDraft, PeopleImportRow
from apps.people_imports.presenters import draft_to_dict, row_to_dict
from apps.people_imports.services import (
    create_draft,
    discard_draft,
    get_owned_draft,
    mark_dispatch_failed,
    owned_drafts,
    prepare_confirmation,
    update_rows,
)
from core.api_auth import check_perm, require_auth
from core.exceptions import ServiceUnavailableException, ValidationException
from core.http import int_field, read_json, reject_unknown_fields
from core.listing import paginate
from core.ratelimit import check_rate
from core.responses import created, error, no_content, paginated, success
from core.utils import current_schema

_KINDS = frozenset(PeopleImportDraft.Kind.values)
_STATUSES = frozenset(PeopleImportDraft.Status.values)
_ACTIVE_STATUSES = (
    PeopleImportDraft.Status.DRAFT,
    PeopleImportDraft.Status.QUEUED,
    PeopleImportDraft.Status.PROCESSING,
    PeopleImportDraft.Status.NEEDS_ATTENTION,
    PeopleImportDraft.Status.FAILED,
)


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _kind(value: object) -> str:
    candidate = str(value or "").strip().lower()
    if candidate not in _KINDS:
        raise ValidationException(
            "Choose students or teachers.",
            code="validation_error",
            fields={"kind": ["Choose students or teachers."]},
        )
    return candidate


def _permission(kind: str, verb: str) -> str:
    return f"{kind}s:{verb}"


@csrf_exempt
@require_auth
def imports_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        kind = _kind(request.GET.get("kind"))
        check_perm(request, _permission(kind, "read"))
        queryset = owned_drafts(request).filter(kind=kind)
        status = str(request.GET.get("status") or "active").strip().lower()
        if status == "active":
            queryset = queryset.filter(status__in=_ACTIVE_STATUSES)
        elif status == "all":
            pass
        elif status in _STATUSES:
            queryset = queryset.filter(status=status)
        else:
            raise ValidationException(
                "Invalid import status.",
                code="validation_error",
                fields={"status": ["Choose active, all, or a valid import status."]},
            )
        items, total, page, page_size = paginate(request, queryset, default_size=8)
        return paginated(
            [draft_to_dict(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    if request.method == "POST":
        kind = _kind(request.POST.get("kind"))
        check_perm(request, _permission(kind, "write"))
        check_rate(
            scope="people_import_upload",
            key=f"{current_schema()}:{request.user.pk}",
            limit=8,
            window=60,
        )
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationException(
                "Choose a file to import.",
                code="validation_error",
                fields={"file": ["Choose a CSV, TSV, or XLSX file."]},
            )
        default_branch = int_field(
            {"default_branch": request.POST.get("default_branch") or None},
            "default_branch",
            min_value=1,
        )
        draft = create_draft(
            request=request,
            kind=kind,
            file_obj=upload,
            default_branch_id=default_branch,
        )
        return created(draft_to_dict(draft), message="The file is ready to review.")
    return _method_not_allowed()


@csrf_exempt
@require_auth
def import_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    draft = get_owned_draft(request, pk, lock=False)
    permission = _permission(draft.kind, "read" if request.method in ("GET", "HEAD") else "write")
    check_perm(request, permission)
    if request.method in ("GET", "HEAD"):
        return success(draft_to_dict(draft))
    if request.method == "PATCH":
        body = read_json(request)
        reject_unknown_fields(body, allowed={"rows"})
        draft = update_rows(request=request, draft_id=pk, changes=body.get("rows"))
        return success(draft_to_dict(draft), message="Draft changes saved.")
    if request.method == "DELETE":
        discard_draft(request=request, draft_id=pk)
        return no_content()
    return _method_not_allowed()


@csrf_exempt
@require_auth
def import_rows_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    draft = get_owned_draft(request, pk, lock=False)
    check_perm(request, _permission(draft.kind, "read"))
    queryset = draft.rows.all()
    state = str(request.GET.get("state") or "all").strip().lower()
    if state == "all":
        pass
    elif state == "excluded":
        queryset = queryset.filter(is_included=False)
    elif state in PeopleImportRow.State.values:
        queryset = queryset.filter(is_included=True, state=state)
    else:
        raise ValidationException(
            "Invalid row state.",
            code="validation_error",
            fields={"state": ["Choose all, ready, invalid, imported, or excluded."]},
        )
    items, total, page, page_size = paginate(request, queryset, default_size=50)
    return paginated(
        [row_to_dict(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@csrf_exempt
@require_auth
def import_confirm_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    draft = get_owned_draft(request, pk, lock=False)
    check_perm(request, _permission(draft.kind, "write"))
    body = read_json(request)
    reject_unknown_fields(body, allowed={"confirmed"})
    if body.get("confirmed") is not True:
        raise ValidationException(
            "Confirm this permanent account creation.",
            code="confirmation_required",
            fields={"confirmed": ["Set confirmed to true after showing the warning."]},
        )
    check_rate(
        scope="people_import_confirm",
        key=f"{current_schema()}:{request.user.pk}",
        limit=6,
        window=60,
    )
    draft = prepare_confirmation(request=request, draft_id=pk)
    try:
        from celery_tasks.people_import_tasks import process_people_import

        process_people_import.delay(draft.pk, _schema_name=current_schema())
    except Exception as exc:
        mark_dispatch_failed(draft.pk)
        raise ServiceUnavailableException(
            "The import queue is temporarily unavailable. Your draft is safe; try again.",
            code="import_queue_unavailable",
        ) from exc
    draft.refresh_from_db()
    return success(draft_to_dict(draft), message="Account creation has started.", status=202)
