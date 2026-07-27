"""Logging filters + formatters.

`TenantSchemaFilter` / `RequestIDFilter` enrich every record with the active
tenant schema and the current request id (set by `core.middleware`).
`JsonFormatter` renders structured single-line JSON for production — no new
dependency, stays within TD-16.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar

from django.db import connection

# Holds the current request's id for the life of the request. Set by
# core.middleware.RequestIDMiddleware; read by RequestIDFilter so log lines
# emitted anywhere in the stack carry the correlation id.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class TenantSchemaFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.schema = connection.schema_name  # type: ignore[attr-defined]
        except Exception:
            record.schema = "-"
        return True


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """Structured JSON log lines for production (D1-LA-10).

    Keys: ts, level, logger, msg, schema, request_id. `schema`/`request_id`
    are injected by the filters above; default to "-" when a record predates
    them (e.g. early boot).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "schema": getattr(record, "schema", "-"),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)
