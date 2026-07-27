"""Storage-usage generator (D4-LB-3): stored file bytes + counts by library.

Sums ``content.LessonFile.size_bytes`` over CLEAN files (the same population
``apps.content.selectors.storage_used_bytes`` meters). Grouped by the owning
content library so a director can see where storage is going.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, F, Sum

from apps.content.models import LessonFile
from apps.reports.generators.base import ReportGenerator


class StorageUsageGenerator(ReportGenerator):
    key = "storage_usage"
    title = "Storage usage report"
    template_base = "storage_usage"

    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
        clean = LessonFile.objects.filter(status=LessonFile.Status.CLEAN)

        # A file hangs off a lesson (module->course->library) OR a folder
        # (folder->library). Resolve the library id from whichever is set; one
        # grouped query each, then merge — keeps the SQL simple and N+1-free.
        lesson_rows = (
            clean.filter(lesson__isnull=False)
            .values(
                lib_id=F("lesson__module__course__library_id"),
                lib_name=F("lesson__module__course__library__name"),
            )
            .annotate(bytes=Sum("size_bytes"), files=Count("id"))
        )
        folder_rows = (
            clean.filter(folder__isnull=False)
            .values(
                lib_id=F("folder__library_id"),
                lib_name=F("folder__library__name"),
            )
            .annotate(bytes=Sum("size_bytes"), files=Count("id"))
        )

        merged: dict[int, dict[str, Any]] = {}
        for src in (lesson_rows, folder_rows):
            for r in src:
                lib_id = r["lib_id"]
                entry = merged.setdefault(lib_id, {"library": r["lib_name"] or "", "bytes": 0, "files": 0})
                entry["bytes"] += int(r["bytes"] or 0)
                entry["files"] += int(r["files"] or 0)

        rows = [
            {"library": e["library"], "bytes": e["bytes"], "files": e["files"]}
            for e in sorted(merged.values(), key=lambda e: -e["bytes"])
        ]
        total_bytes = clean.aggregate(s=Sum("size_bytes"))["s"] or 0
        total_files = clean.aggregate(c=Count("id"))["c"] or 0
        return {
            "columns": ["library", "files", "bytes"],
            "rows": rows,
            "total_bytes": int(total_bytes),
            "total_files": int(total_files),
        }
