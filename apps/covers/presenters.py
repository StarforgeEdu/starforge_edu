"""Cover-request presenters — plain dict mappers (replace the DRF serializer)."""

from __future__ import annotations

from typing import Any

from apps.covers.models import CoverRequest


def cover_to_dict(c: CoverRequest) -> dict[str, Any]:
    return {
        "id": c.id,
        "lesson": c.lesson_id,
        "requester": c.requester_id,
        "reason": c.reason,
        "status": c.status,
        "pool": c.pool,
        "cover_teacher": c.cover_teacher_id,
        "branch": c.branch_id,
        "decided_by": c.decided_by_id,
        "decided_at": c.decided_at.isoformat() if c.decided_at else None,
        "created_at": c.created_at.isoformat(),
    }
