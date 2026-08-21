"""Placement read selectors (F1-7) — model-less, compute-on-read group suggestion.

After a lead is auto-leveled (F1-6), suggest which cohorts in their branch they
could join: a TRANSPARENT rule (same branch, not archived, not ended, has a free
seat) ranked so an exact level match floats to the top. It only suggests — the
lead may stay groupless or leave; reception/a manager makes the actual call.
"""

from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone

from apps.cohorts.models import Cohort


def suggest_cohorts(*, student, today=None) -> list[dict]:
    today = today or timezone.localdate()
    level = (student.academic_level or "").strip().lower()
    cohorts = Cohort.objects.filter(
        branch_id=student.branch_id, is_archived=False, end_date__gte=today
    ).annotate(active_members=Count("memberships", filter=Q(memberships__end_date__isnull=True)))
    suggestions: list[dict] = []
    for cohort in cohorts:
        # capacity None = uncapped (always a free seat); otherwise skip a full group.
        seats_available = None if cohort.capacity is None else cohort.capacity - cohort.active_members
        if seats_available is not None and seats_available <= 0:
            continue
        suggestions.append(
            {
                "cohort_id": cohort.id,
                "name": cohort.name,
                "level": cohort.level,
                "level_match": bool(level) and cohort.level.strip().lower() == level,
                "seats_available": seats_available,
                "start_date": cohort.start_date,
            }
        )
    # Exact level matches first, then the soonest-starting group.
    suggestions.sort(key=lambda row: (not row["level_match"], row["start_date"]))
    return suggestions
