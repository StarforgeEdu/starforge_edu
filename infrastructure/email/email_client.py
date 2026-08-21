"""Thin wrapper over django.core.mail.send_mail."""

from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.utils.translation import gettext_lazy as _

from core.exceptions import ServiceUnavailableException


def send_email(*, to: str, subject: str, body: str, html: str | None = None) -> None:
    if not getattr(settings, "EMAIL_ENABLED", True):
        raise ServiceUnavailableException(
            _("Email delivery is temporarily unavailable."),
            code="email_unavailable",
        )
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to],
        html_message=html,
        fail_silently=False,
    )
