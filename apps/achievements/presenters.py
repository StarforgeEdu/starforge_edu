"""Achievement-domain presenters — plain dict mappers (replace the DRF serializers)."""

from __future__ import annotations

from typing import Any

from apps.achievements.models import Achievement, AchievementGrant


def achievement_to_dict(a: Achievement) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "description": a.description,
        "emoji": a.emoji,
        "scope": a.scope,
        "cohort": a.cohort_id,
        "branch": a.branch_id,
        "status": a.status,
        "created_by": a.created_by_id,
        "decided_by": a.decided_by_id,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "created_at": a.created_at.isoformat(),
    }


def achievement_grant_to_dict(g: AchievementGrant) -> dict[str, Any]:
    return {
        "id": g.id,
        "achievement": g.achievement_id,
        "achievement_detail": achievement_to_dict(g.achievement),
        "student": g.student_id,
        "granted_by": g.granted_by_id,
        "note": g.note,
        "granted_at": g.granted_at.isoformat(),
    }
