"""Eskiz SMS client + dev mock.

Eskiz is the dominant Uzbekistan SMS gateway. Real client uses email/password
to obtain a JWT, then POSTs to /message/sms/send. Mock just logs.

Throttling is handled upstream by the OTP throttle classes; this client
trusts the caller and dispatches.

TD-17 fixes:
- The 401 handler re-authenticates exactly once and retries; a second 401 is a
  real auth failure and is raised, instead of recursing without a guard.
- The sender ID is read from ``settings.ESKIZ_FROM`` (was hardcoded ``"4546"``).
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from functools import lru_cache
from typing import Any, ClassVar

import requests
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from core.exceptions import ServiceUnavailableException

logger = logging.getLogger("starforge.sms")

_UZ_E164_PHONE = re.compile(r"\+998[0-9]{9}\Z")
_MAX_SMS_TEXT_CHARS = 4096
_MAX_SMS_TEXT_BYTES = 8 * 1024
_ACCEPTED_SEND_STATUSES = frozenset({"accepted", "ok", "queued", "sent", "success", "waiting"})


def _validate_sms_payload(*, phone: str, text: str) -> tuple[str, str]:
    """Validate the final provider payload at the outbound trust boundary.

    User/profile validation normally happens much earlier, but background jobs
    can outlive the row that produced them and test doubles can call the client
    directly.  Keep real and mock transports behaviourally identical and never
    let an unbounded message or a surprising ``lstrip('+')`` transformation
    reach the paid provider.
    """

    if not isinstance(phone, str) or _UZ_E164_PHONE.fullmatch(phone) is None:
        raise ValueError("Eskiz phone must be a canonical Uzbekistan E.164 number")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("SMS text must be a non-blank string")
    if len(text) > _MAX_SMS_TEXT_CHARS or len(text.encode("utf-8")) > _MAX_SMS_TEXT_BYTES:
        raise ValueError("SMS text exceeds the provider safety limit")
    if "\x00" in text:
        raise ValueError("SMS text contains a null character")
    return phone[1:], text


def _safe_send_receipt(data: Any) -> dict[str, str]:
    """Reduce Eskiz's untrusted response to a small non-PII delivery receipt."""

    from infrastructure.http_client import InvalidProviderResponse

    if not isinstance(data, dict):
        raise InvalidProviderResponse("Eskiz returned a non-object send response.")
    raw_status = data.get("status")
    if not isinstance(raw_status, str):
        raise InvalidProviderResponse("Eskiz returned an invalid send status.")
    status = raw_status.strip().lower()
    if status not in _ACCEPTED_SEND_STATUSES:
        # Some providers return HTTP 200 for a rejected business operation. Do
        # not turn that body into a SENT delivery merely because transport
        # succeeded.
        raise InvalidProviderResponse("Eskiz did not accept the SMS for delivery.")
    receipt = {"status": status}
    for key in ("id", "message_id"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        normalized = str(value)
        if (
            normalized
            and len(normalized) <= 128
            and normalized.strip() == normalized
            and not any(ord(char) < 32 or ord(char) == 127 for char in normalized)
        ):
            receipt[key] = normalized
    return receipt


class SMSClient(ABC):
    @abstractmethod
    def send(self, *, phone: str, text: str) -> dict[str, Any]: ...


class MockEskizClient(SMSClient):
    """Deterministic mock used outside production (``ESKIZ_USE_MOCK=True``).

    ``outbox`` is a class-level capture buffer the test suite asserts against
    (see agents/TESTING.md §2); the ``sms_outbox`` fixture clears it per test.
    """

    outbox: ClassVar[list[dict[str, str]]] = []

    def send(self, *, phone: str, text: str) -> dict[str, Any]:
        _validate_sms_payload(phone=phone, text=text)
        # Staging may use realistic family data. Never duplicate the destination
        # and message body into application logs or an unbounded process outbox.
        logger.info("mock_sms_delivery accepted")
        if getattr(settings, "SMS_MOCK_CAPTURE_OUTBOX", False):
            self.outbox.append({"phone": phone, "text": text})
        return {"status": "ok", "mock": True}


class EskizClient(SMSClient):
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        password: str,
        sender: str,
        allowed_hosts: Iterable[str] | None = None,
    ) -> None:
        from infrastructure.http_client import validate_https_endpoint

        if allowed_hosts is None:
            from urllib.parse import urlsplit

            allowed_hosts = (urlsplit(base_url).hostname or "",)
        self.allowed_hosts = tuple(allowed_hosts)
        self.base_url = validate_https_endpoint(base_url, allowed_hosts=self.allowed_hosts).rstrip("/")
        self.email = email
        self.password = password
        self.sender = sender
        self._token: str | None = None

    def _login(self) -> str:
        from infrastructure.http_client import InvalidProviderResponse, request_json_limited

        data = request_json_limited(
            requests.post,
            f"{self.base_url}/auth/login",
            allowed_hosts=self.allowed_hosts,
            form_body={"email": self.email, "password": self.password},
            timeout=(3.05, 10.0),
            max_response_bytes=64 * 1024,
        )
        try:
            token = data["data"]["token"]
        except (KeyError, TypeError) as exc:
            raise InvalidProviderResponse("Eskiz returned an invalid login response.") from exc
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 4096
            or token.strip() != token
            or any(ord(char) < 32 or ord(char) == 127 for char in token)
        ):
            raise InvalidProviderResponse("Eskiz returned an invalid login credential.")
        self._token = token
        return self._token

    def _auth_header(self) -> dict[str, str]:
        if self._token is None:
            self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def _post_message(self, *, phone: str, text: str) -> dict[str, Any]:
        # phone must be in 998XXXXXXXXX format for Eskiz (no leading +)
        eskiz_phone, text = _validate_sms_payload(phone=phone, text=text)
        from infrastructure.http_client import request_json_limited

        result = request_json_limited(
            requests.post,
            f"{self.base_url}/message/sms/send",
            allowed_hosts=self.allowed_hosts,
            form_body={"mobile_phone": eskiz_phone, "message": text, "from": self.sender},
            headers=self._auth_header(),
            timeout=(3.05, 10.0),
            max_response_bytes=64 * 1024,
        )
        return _safe_send_receipt(result)

    def send(self, *, phone: str, text: str) -> dict[str, Any]:
        try:
            return self._post_message(phone=phone, text=text)
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 401:
                raise
            # Token expired — re-authenticate exactly once and retry. A second
            # 401 means the credentials themselves are bad; raise rather than
            # recurse forever (TD-17).
            self._token = None
            self._login()
            return self._post_message(phone=phone, text=text)


def get_sms_client() -> SMSClient:
    if not getattr(settings, "SMS_ENABLED", True):
        raise ServiceUnavailableException(
            _("SMS delivery is temporarily unavailable."),
            code="sms_unavailable",
        )
    if settings.ESKIZ_USE_MOCK:
        return MockEskizClient()
    return _real_sms_client(
        settings.ESKIZ_API_URL,
        settings.ESKIZ_EMAIL,
        settings.ESKIZ_PASSWORD,
        settings.ESKIZ_FROM,
        tuple(getattr(settings, "ESKIZ_API_ALLOWED_HOSTS", ())),
    )


@lru_cache(maxsize=4)
def _real_sms_client(
    base_url: str,
    email: str,
    password: str,
    sender: str,
    allowed_hosts: tuple[str, ...],
) -> EskizClient:
    """Reuse the provider bearer token within a worker process."""
    return EskizClient(
        base_url=base_url,
        email=email,
        password=password,
        sender=sender,
        allowed_hosts=allowed_hosts,
    )
