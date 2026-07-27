"""Branch-transfer endpoints — read-only audit list (D1-LF-6)."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.org.interfaces.services import IBranchTransferService
from apps.org.presenters import transfer_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException
from core.listing import apply_filters, paginate
from core.responses import error, paginated, success

_RESOURCE = "org"
_FILTERS = ("user", "from_branch", "to_branch")
_ORDERING = ("created_at",)


def _service() -> IBranchTransferService:
    return container.resolve(IBranchTransferService)  # type: ignore[type-abstract]


@csrf_exempt
@require_auth
def transfers_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method != "GET":
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    qs = apply_filters(
        request, _service().list(), filter_fields=_FILTERS,
        ordering_fields=_ORDERING, default_ordering="-created_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([transfer_to_dict(t) for t in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_auth
def transfer_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    check_perm(request, f"{_RESOURCE}:read")
    transfer = _service().get(pk)
    if transfer is None:
        raise NotFoundException(code="not_found")
    return success(transfer_to_dict(transfer))
