"""Day-1 consumers of the auth signals: structured log lines.

Day 3 Lane D adds AuditLog receivers alongside these (TD-9). Each receiver
carries a stable ``dispatch_uid`` so re-imports never double-register.
"""

from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.auth.signals import login_failed, login_succeeded, otp_failed, otp_requested, otp_verified
from core.privacy import private_fingerprint

logger = logging.getLogger("starforge.auth")


@receiver(login_succeeded, dispatch_uid="auth.log_login_succeeded")
def on_login_succeeded(sender, *, username, user_id=None, ip="", user_agent="", **kwargs):
    logger.info(
        "login_succeeded user_id=%s username_ref=%s ip_ref=%s ua_ref=%s",
        user_id,
        private_fingerprint(username, namespace="auth-identifier"),
        private_fingerprint(ip, namespace="auth-ip"),
        private_fingerprint(user_agent, namespace="auth-user-agent"),
    )


@receiver(login_failed, dispatch_uid="auth.log_login_failed")
def on_login_failed(sender, *, username, ip="", user_agent="", reason="", **kwargs):
    logger.warning(
        "login_failed username_ref=%s ip_ref=%s ua_ref=%s reason=%s",
        private_fingerprint(username, namespace="auth-identifier"),
        private_fingerprint(ip, namespace="auth-ip"),
        private_fingerprint(user_agent, namespace="auth-user-agent"),
        reason,
    )


@receiver(otp_requested, dispatch_uid="auth.log_otp_requested")
def on_otp_requested(sender, *, identifier, purpose="", ip="", user_agent="", **kwargs):
    logger.info(
        "otp_requested identifier_ref=%s purpose=%s ip_ref=%s ua_ref=%s",
        private_fingerprint(identifier, namespace="auth-identifier"),
        purpose,
        private_fingerprint(ip, namespace="auth-ip"),
        private_fingerprint(user_agent, namespace="auth-user-agent"),
    )


@receiver(otp_verified, dispatch_uid="auth.log_otp_verified")
def on_otp_verified(sender, *, identifier, purpose="", ip="", user_agent="", **kwargs):
    logger.info(
        "otp_verified identifier_ref=%s purpose=%s ip_ref=%s ua_ref=%s",
        private_fingerprint(identifier, namespace="auth-identifier"),
        purpose,
        private_fingerprint(ip, namespace="auth-ip"),
        private_fingerprint(user_agent, namespace="auth-user-agent"),
    )


@receiver(otp_failed, dispatch_uid="auth.log_otp_failed")
def on_otp_failed(sender, *, identifier, ip="", user_agent="", reason="", **kwargs):
    logger.warning(
        "otp_failed identifier_ref=%s ip_ref=%s ua_ref=%s reason=%s",
        private_fingerprint(identifier, namespace="auth-identifier"),
        private_fingerprint(ip, namespace="auth-ip"),
        private_fingerprint(user_agent, namespace="auth-user-agent"),
        reason,
    )
