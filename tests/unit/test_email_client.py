from __future__ import annotations

import pytest

from core.exceptions import ServiceUnavailableException
from infrastructure.email.email_client import send_email


def test_disabled_email_fails_closed_before_django_transport(monkeypatch, settings):
    settings.EMAIL_ENABLED = False
    monkeypatch.setattr(
        "infrastructure.email.email_client.send_mail",
        lambda **kwargs: pytest.fail("disabled email transport was called"),
    )

    with pytest.raises(ServiceUnavailableException) as exc_info:
        send_email(to="person@example.com", subject="subject", body="body")

    assert exc_info.value.code == "email_unavailable"
