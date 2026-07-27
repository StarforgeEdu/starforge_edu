"""Renderer that puts DRF responses in the project's standard envelope.

The whole API answers in ONE shape (see ``core.responses``):

    success:     {"success": true, "data": <payload>}
    paginated:   {"success": true, "data": [...], "pagination": {...}}
    error:       {"success": false, "code": ..., "message": ...}

Every app is off-DRF and builds these by hand — except the lone remaining DRF app
(``apps.reports``), whose ViewSets return bare serializer data and DRF-native
``{count, next, previous, results}`` pagination. Attaching this renderer to those
ViewSets re-wraps their bodies into the same envelope, so a client integrating the
API never has to special-case one mount — and the availability middleware, which
injects degraded-mode ``warnings`` only into bodies carrying a top-level
``success`` key, now covers reports too.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rest_framework.renderers import JSONRenderer


class StandardEnvelopeRenderer(JSONRenderer):
    """Wrap DRF ``Response.data`` in the standard success/paginated envelope.

    Error bodies (already flat ``{"success": false, ...}`` from
    ``core.exceptions.drf_exception_handler``) and any body already carrying a
    ``success`` key pass through untouched.
    """

    def render(
        self,
        data: Any,
        accepted_media_type: str | None = None,
        renderer_context: Mapping[str, Any] | None = None,
    ) -> bytes:
        ctx = renderer_context or {}
        response = ctx.get("response")
        status_code = getattr(response, "status_code", 200)
        wrapped = self._wrap(data, status_code, ctx.get("view"))
        return super().render(wrapped, accepted_media_type, renderer_context)

    @staticmethod
    def _wrap(data: Any, status_code: int, view: Any) -> Any:
        # Already enveloped (error handler, or a view that built its own envelope).
        if isinstance(data, dict) and "success" in data:
            return data
        # 4xx/5xx bodies are shaped by drf_exception_handler; leave them alone.
        if status_code >= 400:
            return data
        # DRF PageNumberPagination body -> our {data, pagination} shape. The paginator
        # on the view still holds the page object, so total/page/page_size are exact.
        if isinstance(data, dict) and "results" in data and "count" in data:
            page = getattr(getattr(view, "paginator", None), "page", None)
            if page is not None:
                total = page.paginator.count
                page_size = page.paginator.per_page
                number = page.number
                pages = page.paginator.num_pages
            else:  # defensive: paginator without an active page
                total = data.get("count") or 0
                page_size = len(data["results"]) or 1
                number = 1
                pages = (total + page_size - 1) // page_size if page_size else 0
            return {
                "success": True,
                "data": data["results"],
                "pagination": {
                    "total": total,
                    "page": number,
                    "page_size": page_size,
                    "pages": pages,
                    "has_next": data.get("next") is not None,
                    "has_prev": data.get("previous") is not None,
                },
            }
        return {"success": True, "data": data}
