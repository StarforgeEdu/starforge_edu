"""Firebase Cloud Messaging (FCM) push client + dev mock (D3-C-6, TD-2).

Pattern mirrors ``infrastructure/sms/eskiz_client.py``: an ABC, a deterministic
pure-Python mock used outside production (``FCM_USE_MOCK`` default True), a real
client that imports ``firebase_admin`` **lazily** inside the method that needs it
(the lib ships GTK-free but is an optional/heavy dep that is NOT installed on the
dev box — never import it at module level), and a settings factory.

``[OWNER:O-7]`` — real FCM credentials. Until they arrive everything runs against
``MockFCMClient`` per TD-2. The mock NEVER imports firebase_admin, so the module
loads and the app boots with the lib absent.

Return shape (both clients): ``{"success": bool, "message_id": str|None,
"error": str|None}`` — the dispatch task records it verbatim in
``NotificationDelivery.provider_response`` and uses ``success`` to drive the
3-consecutive-failures dead-token cleanup (D3-C-11).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from django.conf import settings

logger = logging.getLogger("starforge.push")


class PushClient(ABC):
    @abstractmethod
    def send(
        self, *, token: str, title: str, body: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one push notification to a single device token."""
        ...


class MockFCMClient(PushClient):
    """Deterministic mock used outside production (``FCM_USE_MOCK=True``).

    ``outbox`` is a class-level capture buffer the test suite asserts against
    (same contract as ``MockEskizClient.outbox``). A token literally containing
    ``"dead"`` reports failure so the dead-token cleanup path (D3-C-11) is
    exercisable deterministically without a network.
    """

    outbox: ClassVar[list[dict[str, Any]]] = []

    def send(
        self, *, token: str, title: str, body: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        logger.info("[MOCK FCM] token=%s title=%s", token[:12], title)
        if not token or "dead" in token:
            result = {"success": False, "message_id": None, "error": "unregistered", "mock": True}
        else:
            # Deterministic message id derived from the input — no randomness.
            result = {
                "success": True,
                "message_id": f"mock-fcm-{abs(hash((token, title, body))) % 10**12:012d}",
                "error": None,
                "mock": True,
            }
        self.outbox.append({"token": token, "title": title, "body": body, "data": data or {}, **result})
        return result


class FCMClient(PushClient):
    """Real FCM client. ``firebase_admin`` is imported LAZILY in ``send`` so the
    module is importable (and the whole app boots) with the lib absent — only an
    actual real-mode ``send`` requires it installed + credentials configured."""

    _app: ClassVar[Any] = None

    def __init__(self, *, credentials_file: str) -> None:
        self.credentials_file = credentials_file

    def _ensure_app(self) -> Any:
        # Lazy import — see module docstring. firebase_admin is declared in
        # pyproject for CI but not installed on the dev box.
        import firebase_admin
        from firebase_admin import credentials

        if FCMClient._app is None:
            cred = credentials.Certificate(self.credentials_file)
            FCMClient._app = firebase_admin.initialize_app(cred)
        return FCMClient._app

    def send(
        self, *, token: str, title: str, body: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        from firebase_admin import messaging

        self._ensure_app()
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            token=token,
        )
        try:
            message_id = messaging.send(message)
            return {"success": True, "message_id": message_id, "error": None}
        except messaging.UnregisteredError as exc:
            return {"success": False, "message_id": None, "error": "unregistered", "detail": str(exc)}
        except Exception as exc:  # pragma: no cover - real-mode network errors
            logger.warning("FCM send failed: %s", exc)
            return {"success": False, "message_id": None, "error": "send_failed", "detail": str(exc)}


def get_push_client() -> PushClient:
    # Default True so the app boots + tests run mock-first even before the env
    # keys are wired into settings (declared in integration_needed). Outside
    # production this is always the mock per TD-2.
    if getattr(settings, "FCM_USE_MOCK", True):
        return MockFCMClient()
    return FCMClient(credentials_file=settings.FCM_CREDENTIALS_FILE)
