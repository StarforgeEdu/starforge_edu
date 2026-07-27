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
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import requests
from django.conf import settings

logger = logging.getLogger("starforge.sms")


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
        logger.info("[MOCK SMS] phone=%s text=%s", phone, text)
        self.outbox.append({"phone": phone, "text": text})
        return {"status": "ok", "mock": True}


class EskizClient(SMSClient):
    def __init__(self, *, base_url: str, email: str, password: str, sender: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.sender = sender
        self._token: str | None = None

    def _login(self) -> str:
        resp = requests.post(
            f"{self.base_url}/auth/login",
            data={"email": self.email, "password": self.password},
            timeout=10,
        )
        resp.raise_for_status()
        self._token = resp.json()["data"]["token"]
        return self._token

    def _auth_header(self) -> dict[str, str]:
        if self._token is None:
            self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def _post_message(self, *, phone: str, text: str) -> requests.Response:
        # phone must be in 998XXXXXXXXX format for Eskiz (no leading +)
        eskiz_phone = phone.lstrip("+")
        return requests.post(
            f"{self.base_url}/message/sms/send",
            data={"mobile_phone": eskiz_phone, "message": text, "from": self.sender},
            headers=self._auth_header(),
            timeout=10,
        )

    def send(self, *, phone: str, text: str) -> dict[str, Any]:
        resp = self._post_message(phone=phone, text=text)
        if resp.status_code == 401:
            # Token expired — re-authenticate exactly once and retry. A second
            # 401 means the credentials themselves are bad; raise rather than
            # recurse forever (TD-17).
            self._token = None
            self._login()
            resp = self._post_message(phone=phone, text=text)
        resp.raise_for_status()
        return resp.json()


def get_sms_client() -> SMSClient:
    if settings.ESKIZ_USE_MOCK:
        return MockEskizClient()
    return EskizClient(
        base_url=settings.ESKIZ_API_URL,
        email=settings.ESKIZ_EMAIL,
        password=settings.ESKIZ_PASSWORD,
        sender=settings.ESKIZ_FROM,
    )
