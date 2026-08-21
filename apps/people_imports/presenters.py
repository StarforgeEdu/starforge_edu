"""Public response shapes for reviewed people imports."""

from __future__ import annotations

from typing import Any

from apps.people_imports.models import PeopleImportDraft, PeopleImportRow
from apps.people_imports.services import FIELDS_BY_KIND


def draft_to_dict(draft: PeopleImportDraft) -> dict[str, Any]:
    editable = draft.status in {
        PeopleImportDraft.Status.DRAFT,
        PeopleImportDraft.Status.NEEDS_ATTENTION,
        PeopleImportDraft.Status.FAILED,
    }
    return {
        "id": draft.pk,
        "kind": draft.kind,
        "kind_label": draft.get_kind_display(),
        "status": draft.status,
        "status_label": draft.get_status_display(),
        "source_file_name": draft.source_file_name,
        "source_sheet": draft.source_sheet,
        "fields": list(FIELDS_BY_KIND[draft.kind]),
        "row_count": draft.row_count,
        "ready_count": draft.ready_count,
        "error_count": draft.error_count,
        "excluded_count": draft.excluded_count,
        "imported_count": draft.imported_count,
        "remaining_count": max(0, draft.ready_count + draft.error_count),
        "error_message": draft.error_message,
        "can_edit": editable,
        "can_confirm": editable and draft.ready_count > 0 and draft.error_count == 0,
        "created_at": draft.created_at.isoformat(),
        "updated_at": draft.updated_at.isoformat(),
        "started_at": draft.started_at.isoformat() if draft.started_at else None,
        "completed_at": draft.completed_at.isoformat() if draft.completed_at else None,
    }


def row_to_dict(row: PeopleImportRow) -> dict[str, Any]:
    return {
        "id": row.pk,
        "position": row.position,
        "source_data": row.source_data,
        "data": row.data,
        "errors": row.errors,
        "state": row.state,
        "state_label": row.get_state_display(),
        "is_included": row.is_included,
        "created_object_id": row.created_object_id,
        "updated_at": row.updated_at.isoformat(),
    }
