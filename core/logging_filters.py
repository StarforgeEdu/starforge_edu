"""Logging filters + formatters.

`TenantSchemaFilter` / `RequestIDFilter` enrich every record with the active
tenant schema and the current request id (set by `core.middleware`).
`JsonFormatter` renders structured single-line JSON for production — no new
dependency, stays within TD-16.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar

from django.db import connection

# Holds the current request's id for the life of the request. Set by
# core.middleware.RequestIDMiddleware; read by RequestIDFilter so log lines
# emitted anywhere in the stack carry the correlation id.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_CREDENTIAL_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@")
_SENSITIVE_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\b[\"']?\s*[:=]\s*[^\r\n]+"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(password|passphrase|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)"
    r"\b[\"']?\s*[:=]\s*[\"']?[^\s,;\"'\}\]]+"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|signature|secret|api[_-]?key|access[_-]?token|sig|"
    r"x-amz-(?:credential|signature|security-token)|"
    r"x-goog-(?:credential|signature))=)[^&#\s]+"
)
_ICAL_TOKEN_PATH_RE = re.compile(r"(?i)(/api/v1/schedule/ical/)[^/?#\s]+(/?)")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\w)\+?998[\s().-]*\d(?:[\s().-]*\d){8}(?!\w)")


def redact_log_text(value: object) -> str:
    """Remove common credentials and contact identifiers from operational text.

    Call sites should still log privacy-safe identifiers by design. This final
    formatter boundary protects against third-party exception messages and future
    regressions before they reach stdout or an external log collector.
    """

    text = str(value)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    text = _SENSITIVE_HEADER_RE.sub(lambda match: f"{match.group(1)}: [REDACTED]", text)
    text = _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _ICAL_TOKEN_PATH_RE.sub(r"\1[REDACTED_FEED_TOKEN]\2", text)
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    return _PHONE_RE.sub("[REDACTED_PHONE]", text)


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
            "msg": redact_log_text(record.getMessage()),
            "schema": getattr(record, "schema", "-"),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = redact_log_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)
