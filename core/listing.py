"""List-endpoint helpers for the layered (plain-view) style — the filtering, search,
ordering, and pagination that DRF's filter backends + paginator gave a ViewSet, as
composable functions a plain ``list_view`` calls before handing the page to a presenter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.core.exceptions import FieldError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.db.models import Q, QuerySet
from django.http import HttpRequest

from core.exceptions import ValidationException
from core.http import parse_bool

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
# A page whose offset would exceed this is treated as past-the-end (empty) rather
# than passed to the DB — Postgres OFFSET is a bigint and a giant ?page overflows it.
_MAX_OFFSET = 1_000_000_000


def apply_filters(
    request: HttpRequest,
    queryset: QuerySet,
    *,
    filter_fields: Sequence[str] = (),
    search_fields: Sequence[str] = (),
    ordering_fields: Sequence[str] = (),
    default_ordering: str | None = None,
) -> QuerySet:
    """Apply ``?<field>=`` exact filters, ``?search=`` (icontains across
    ``search_fields``), and ``?ordering=`` (whitelisted to ``ordering_fields``,
    leading ``-`` for desc). Unknown ordering falls back to ``default_ordering``."""
    for field in filter_fields:
        raw = request.GET.get(field)
        if not raw:
            continue
        value: Any = raw
        # Coerce a boolean query param ("true"/"false"/"1"/"0") — Django's model
        # BooleanField rejects lowercase "true" and would raise ValidationError.
        try:
            model_field = queryset.model._meta.get_field(field.split("__")[0])
        except Exception:
            model_field = None
        if isinstance(model_field, models.BooleanField):
            try:
                value = parse_bool(raw, field)
            except ValidationException:
                raise _bad_filter(field) from None
        elif "\x00" in raw:
            raise _bad_filter(field)  # NUL bytes crash psycopg at bind time
        # A bad value for a typed field raises at query-build time — ValueError for an
        # int/FK, Django's ValidationError for a date/datetime/uuid — turn either into a
        # clean 400 instead of a leaked 500.
        try:
            queryset = queryset.filter(**{field: value})
        except (ValueError, FieldError, ValidationException, DjangoValidationError):
            raise _bad_filter(field) from None

    term = request.GET.get("search")
    if term and search_fields:
        if "\x00" in term:
            raise _bad_filter("search")
        clause = Q()
        for field in search_fields:
            clause |= Q(**{f"{field}__icontains": term})
        queryset = queryset.filter(clause)

    ordering = request.GET.get("ordering")
    if ordering:
        # Strip at most ONE leading "-" (descending). ``lstrip("-")`` would strip every
        # dash, so "--field" would pass the whitelist yet reach order_by() as "--field"
        # -> an unmapped FieldError (500) on a field named "-field". Peel a single sign.
        field_name = ordering[1:] if ordering.startswith("-") else ordering
        if field_name in ordering_fields:
            return queryset.order_by(ordering)
    if default_ordering is not None:
        return queryset.order_by(default_ordering)
    return queryset


def _ensure_total_order(queryset: QuerySet) -> QuerySet:
    """Guarantee a deterministic TOTAL order before OFFSET/LIMIT slicing.

    Offset pagination over a non-unique sort column silently drops AND duplicates
    rows across page boundaries: two rows sharing the sort value can swap places
    between the two SELECTs that fetch consecutive pages, so one is returned on both
    pages and another on neither. Appending the primary key as a final tiebreaker
    makes the order total and the paging stable. The pk is indexed, so the extra
    sort key is effectively free even at scale.
    """
    pk_name = queryset.model._meta.pk.name
    ordering = list(queryset.query.order_by) or list(queryset.model._meta.ordering)
    for term in ordering:
        # Only a bare string term can BE the primary key; expression terms
        # (OrderBy/F) and traversals (``author__id``) never guarantee row-uniqueness
        # for THIS model, so they don't count as a tiebreaker.
        if isinstance(term, str) and term.lstrip("-") in (pk_name, "pk", "id"):
            return queryset  # already totally ordered
    return queryset.order_by(*ordering, "pk")


def paginate(
    request: HttpRequest, queryset: QuerySet, *, default_size: int = DEFAULT_PAGE_SIZE
) -> tuple[list[Any], int, int, int]:
    """Slice ``queryset`` by ``?page`` / ``?page_size`` (size capped at MAX_PAGE_SIZE).
    Returns ``(items, total, page, page_size)``; counts before slicing for the meta.
    Pass the result to ``core.responses.paginated`` after mapping items to dicts."""
    page = _positive_int(request.GET.get("page"), 1)
    size = min(_positive_int(request.GET.get("page_size"), default_size), MAX_PAGE_SIZE)
    total = queryset.count()
    start = (page - 1) * size
    if start > _MAX_OFFSET:
        # Past-the-end (a giant ?page): return an empty page instead of overflowing
        # the DB's bigint OFFSET with a 500.
        return [], total, page, size
    queryset = _ensure_total_order(queryset)
    return list(queryset[start : start + size]), total, page, size


def cursor_paginate(
    request: HttpRequest, queryset: QuerySet, *, page_size: int = 50, max_page_size: int = MAX_PAGE_SIZE
) -> tuple[list[Any], str | None, str | None]:
    """Keyset cursor pagination for an append-only timeline ordered ``(-created_at, -id)``.

    Stable under concurrent head-inserts (unlike offset pagination) — what an audit /
    activity feed needs: a ``?cursor`` walks the timeline by the (created_at, id) of the
    edge row, so newer rows inserted at the head between page reads never shift a page.
    Pure Django (no DRF): the opaque cursor is ``base64("<dir>|<iso>|<id>")``.

    ``queryset`` MUST already be ordered ``(-created_at, -id)``. Returns
    ``(rows, next_link, previous_link)`` — the links are absolute URLs (or ``None``)
    carrying the ``?cursor`` and preserving the request's other query params (filters).
    """
    size = min(_positive_int(request.GET.get("page_size"), page_size), max_page_size)
    direction, ts, obj_id = "f", None, None
    token = request.GET.get("cursor")
    if token:
        direction, ts, obj_id = _decode_cursor(token)

    if direction == "b":
        # Walk backwards (towards NEWER rows): fetch ascending past the cursor, then
        # re-present newest-first so the page reads in the timeline's native order.
        rows = list(
            queryset.filter(Q(created_at__gt=ts) | Q(created_at=ts, id__gt=obj_id)).order_by(
                "created_at", "id"
            )[: size + 1]
        )
        has_more = len(rows) > size
        rows = rows[:size]
        rows.reverse()
        has_next, has_previous = True, has_more
    else:
        qs = queryset
        if ts is not None:  # forward from a cursor -> strictly OLDER rows
            qs = qs.filter(Q(created_at__lt=ts) | Q(created_at=ts, id__lt=obj_id))
        rows = list(qs[: size + 1])
        has_more = len(rows) > size
        rows = rows[:size]
        # A forward cursor means newer rows exist (the page we came from) -> has_previous.
        has_next, has_previous = has_more, ts is not None

    next_link = _cursor_link(request, "f", rows[-1]) if (rows and has_next) else None
    previous_link = _cursor_link(request, "b", rows[0]) if (rows and has_previous) else None
    return rows, next_link, previous_link


def _cursor_link(request: HttpRequest, direction: str, row: Any) -> str:
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    token = _encode_cursor(direction, row.created_at, row.id)
    parts = urlparse(request.build_absolute_uri())
    query = parse_qs(parts.query)
    query["cursor"] = [token]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def _encode_cursor(direction: str, created_at: Any, obj_id: int) -> str:
    import base64

    raw = f"{direction}|{created_at.isoformat()}|{obj_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(token: str) -> tuple[str, Any, int]:
    import base64
    import binascii

    from django.utils.dateparse import parse_datetime

    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        direction, iso, raw_id = raw.split("|")
        created_at = parse_datetime(iso)
        if direction not in ("f", "b") or created_at is None:
            raise ValueError
        return direction, created_at, int(raw_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise _bad_filter("cursor") from exc


def _positive_int(raw: str | None, fallback: int) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return value if value >= 1 else fallback


def _bad_filter(field: str) -> ValidationException:
    return ValidationException(
        f"Invalid value for filter '{field}'.",
        code="validation_error",
        fields={field: ["Invalid value."]},
    )
