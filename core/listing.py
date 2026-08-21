"""List-endpoint helpers for the layered (plain-view) style — the filtering, search,
ordering, and pagination that DRF's filter backends + paginator gave a ViewSet, as
composable functions a plain ``list_view`` calls before handing the page to a presenter.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Any

from django.core.exceptions import FieldDoesNotExist, FieldError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from core.exceptions import ValidationException
from core.http import parse_bool

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_SEARCH_LENGTH = 200
# A page whose offset would exceed this is treated as past-the-end (empty) rather
# than passed to the DB — Postgres OFFSET is a bigint and a giant ?page overflows it.
_MAX_OFFSET = 1_000_000_000
_MAX_BIGINT_ID = 9_223_372_036_854_775_807
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def positive_int_filter(request: HttpRequest, name: str) -> int | None:
    """Parse an optional positive-integer query filter without silent fallback.

    An absent or empty filter remains optional. Any supplied non-integer, zero, or
    negative value is a field-scoped 400 so a mistyped CEO filter cannot quietly
    show an unintended register.
    """
    raw = _single_query_value(request, name)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _bad_filter(name) from None
    if value < 1:
        raise _bad_filter(name)
    return value


def validate_pagination_filters(
    request: HttpRequest,
    *,
    max_page_size: int = MAX_PAGE_SIZE,
) -> None:
    """Reject malformed or oversized explicit pagination values.

    ``paginate`` retains its compatibility fallback for legacy endpoints. New
    decision-critical registers call this first so ``?page=oops``, ``?page=0``,
    and an unsupported page size cannot silently turn into a different request.
    """

    _bounded_positive_query(request, "page")
    _bounded_positive_query(request, "page_size", maximum=max_page_size)


def _bounded_positive_query(
    request: HttpRequest,
    name: str,
    *,
    maximum: int | None = None,
) -> int | None:
    raw = _single_query_value(request, name)
    if raw is None:
        return None
    if raw == "":
        raise _bad_filter(name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _bad_filter(name) from None
    if value < 1:
        raise _bad_filter(name)
    if maximum is not None and value > maximum:
        raise _filter_error(name, f"Must be at most {maximum}.")
    return value


def parse_date_range_filters(request: HttpRequest) -> tuple[date | None, date | None]:
    """Parse optional inclusive ``date_from`` / ``date_to`` ISO-date filters."""
    date_from = _parse_date_filter(_single_query_value(request, "date_from"), "date_from")
    date_to = _parse_date_filter(_single_query_value(request, "date_to"), "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise _filter_error("date_to", "Must be on or after date_from.")
    return date_from, date_to


def date_range_datetime_bounds(
    date_from: date | None, date_to: date | None
) -> tuple[datetime | None, datetime | None]:
    """Convert local inclusive dates to timezone-aware datetime boundaries."""
    current_timezone = timezone.get_current_timezone()
    lower = (
        timezone.make_aware(datetime.combine(date_from, time.min), current_timezone)
        if date_from is not None
        else None
    )
    upper = (
        timezone.make_aware(datetime.combine(date_to, time.max), current_timezone)
        if date_to is not None
        else None
    )
    return lower, upper


def apply_date_range_filters(
    queryset: QuerySet,
    *,
    field: str,
    date_from: date | None,
    date_to: date | None,
    datetime_field: bool = False,
) -> QuerySet:
    """Intersect a queryset with an inclusive date range on a date/datetime field."""
    lower: date | datetime | None
    upper: date | datetime | None
    if datetime_field:
        lower, upper = date_range_datetime_bounds(date_from, date_to)
    else:
        lower, upper = date_from, date_to
    if lower is not None:
        queryset = queryset.filter(**{f"{field}__gte": lower})
    if upper is not None:
        queryset = queryset.filter(**{f"{field}__lte": upper})
    return queryset


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
    leading ``-`` for desc). An explicitly unsupported search or ordering value
    is a field-scoped 400 rather than a misleading default result."""
    for field in filter_fields:
        raw = _single_query_value(request, field)
        if not raw:
            continue
        value: Any = raw
        # Coerce a boolean query param ("true"/"false"/"1"/"0") — Django's model
        # BooleanField rejects lowercase "true" and would raise ValidationError.
        model_field = _resolve_filter_model_field(queryset.model, field)
        if isinstance(model_field, models.BooleanField):
            try:
                value = parse_bool(raw, field)
            except ValidationException:
                raise _bad_filter(field) from None
        elif "\x00" in raw:
            raise _bad_filter(field)  # NUL bytes crash psycopg at bind time
        elif model_field is not None and model_field.choices:
            allowed_values = {str(choice) for choice, _label in model_field.flatchoices}
            if raw not in allowed_values:
                raise _bad_filter(field)
        # A bad value for a typed field raises at query-build time — ValueError for an
        # int/FK, Django's ValidationError for a date/datetime/uuid — turn either into a
        # clean 400 instead of a leaked 500.
        try:
            queryset = queryset.filter(**{field: value})
        except (ValueError, FieldError, ValidationException, DjangoValidationError):
            raise _bad_filter(field) from None

    term = _single_query_value(request, "search")
    if term and not search_fields:
        raise _bad_filter("search")
    if term:
        if "\x00" in term or len(term) > MAX_SEARCH_LENGTH:
            raise _bad_filter("search")
        clause = Q()
        for field in search_fields:
            clause |= Q(**{f"{field}__icontains": term})
        queryset = queryset.filter(clause)

    ordering = _single_query_value(request, "ordering")
    if ordering:
        # Strip at most ONE leading "-" (descending). ``lstrip("-")`` would strip every
        # dash, so "--field" would pass the whitelist yet reach order_by() as "--field"
        # -> an unmapped FieldError (500) on a field named "-field". Peel a single sign.
        field_name = ordering[1:] if ordering.startswith("-") else ordering
        if field_name in ordering_fields:
            return queryset.order_by(ordering)
        raise _bad_filter("ordering")
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


def _resolve_filter_model_field(model: type[models.Model], lookup: str) -> Any:
    """Resolve the concrete field at the end of a whitelisted ORM traversal."""
    current_model = model
    resolved = None
    for part in lookup.split("__"):
        try:
            resolved = current_model._meta.get_field(part)
        except FieldDoesNotExist:
            # A lookup suffix (``__date``, ``__gte``) is not itself a model
            # field; validation still applies to the last concrete field.
            return resolved
        related_model = getattr(resolved, "related_model", None)
        if related_model is None:
            return resolved
        current_model = related_model
    return resolved


def paginate(
    request: HttpRequest, queryset: QuerySet, *, default_size: int = DEFAULT_PAGE_SIZE
) -> tuple[list[Any], int, int, int]:
    """Slice ``queryset`` by ``?page`` / ``?page_size`` (size bounded by MAX_PAGE_SIZE).

    Malformed, non-positive, or oversized values are rejected explicitly; the
    API never claims success after silently substituting a different page.
    Returns ``(items, total, page, page_size)``; counts before slicing for the meta.
    Pass the result to ``core.responses.paginated`` after mapping items to dicts."""
    page = _positive_int(_single_query_value(request, "page"), 1, field="page")
    size = _positive_int(
        _single_query_value(request, "page_size"),
        default_size,
        field="page_size",
        max_value=MAX_PAGE_SIZE,
    )
    total = queryset.count()
    start = (page - 1) * size
    if start > _MAX_OFFSET:
        # Past-the-end (a giant ?page): return an empty page instead of overflowing
        # the DB's bigint OFFSET with a 500.
        return [], total, page, size
    queryset = _ensure_total_order(queryset)
    return list(queryset[start : start + size]), total, page, size


def paginate_sequence(
    request: HttpRequest, items: Sequence[Any], *, default_size: int = DEFAULT_PAGE_SIZE
) -> tuple[list[Any], int, int, int]:
    """Bound an already-computed ordered result sequence with the same public paging
    contract as :func:`paginate`. Useful for transparent analytics whose ranking must
    be computed globally before a page can be selected."""
    page = _positive_int(_single_query_value(request, "page"), 1, field="page")
    size = _positive_int(
        _single_query_value(request, "page_size"),
        default_size,
        field="page_size",
        max_value=MAX_PAGE_SIZE,
    )
    total = len(items)
    start = (page - 1) * size
    if start > _MAX_OFFSET:
        return [], total, page, size
    return list(items[start : start + size]), total, page, size


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
    size = _positive_int(
        _single_query_value(request, "page_size"),
        page_size,
        field="page_size",
        max_value=max_page_size,
    )
    direction, ts, obj_id = "f", None, None
    token = _single_query_value(request, "cursor")
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

    if len(token) > 512:
        raise _bad_filter("cursor")
    try:
        # ``urlsafe_b64decode`` silently discards non-alphabet bytes by default.
        # Cursor input is untrusted, so require a canonical URL-safe alphabet and
        # reject whitespace/junk instead of accepting multiple spellings of the
        # same database position.
        raw = base64.b64decode(
            token.encode("ascii"),
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
        direction, iso, raw_id = raw.split("|")
        created_at = parse_datetime(iso)
        obj_id = int(raw_id)
        if (
            direction not in ("f", "b")
            or created_at is None
            or not timezone.is_aware(created_at)
            or obj_id < 1
            or obj_id > _MAX_BIGINT_ID
        ):
            raise ValueError
        return direction, created_at, obj_id
    except (ValueError, binascii.Error, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise _bad_filter("cursor") from exc


def _positive_int(
    raw: str | None,
    fallback: int,
    *,
    field: str,
    max_value: int | None = None,
) -> int:
    if raw is None:
        return fallback
    if raw == "":
        raise _bad_filter(field)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _bad_filter(field) from None
    if value < 1 or (max_value is not None and value > max_value):
        raise _bad_filter(field)
    return value


def _single_query_value(request: HttpRequest, field: str) -> str | None:
    """Return one query value and reject HTTP parameter pollution.

    Django's ``QueryDict.get`` silently chooses the last duplicate. Proxies,
    caches, and generated clients do not all make that same choice, so a signed
    or reviewed management URL must have one unambiguous value per scalar field.
    """
    values = request.GET.getlist(field)
    if len(values) > 1:
        raise _filter_error(field, "Supply this parameter once.")
    return values[0] if values else None


def _parse_date_filter(raw: str | None, field: str) -> date | None:
    if raw is None or raw == "":
        return None
    if _ISO_DATE_RE.fullmatch(raw) is None:
        raise _bad_filter(field)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise _bad_filter(field) from None


def _bad_filter(field: str) -> ValidationException:
    return _filter_error(field, "Invalid value.")


def _filter_error(field: str, message: str) -> ValidationException:
    return ValidationException(
        f"Invalid value for filter '{field}'.",
        code="validation_error",
        fields={field: [message]},
    )
