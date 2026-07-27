"""Auth domain signals (TD-9 precursor).

Fired from `apps.auth.services` / views with flat primitive kwargs
(+ ``schema_name`` for cross-context dispatch). Day 1 consumer is a structured
log line on ``starforge.auth`` (`apps.auth.receivers`); Day 3 Lane D attaches
an AuditLog receiver.

- ``login_succeeded`` / ``login_failed``: username+password login outcomes
  (kwargs: username, user_id?, ip, user_agent, reason?).
- ``otp_requested`` / ``otp_verified`` / ``otp_failed``: password-reset /
  contact-verification codes (kwargs: identifier, purpose, ip, user_agent,
  reason?).
"""

from __future__ import annotations

import django.dispatch

login_succeeded = django.dispatch.Signal()
login_failed = django.dispatch.Signal()

otp_requested = django.dispatch.Signal()
otp_verified = django.dispatch.Signal()
otp_failed = django.dispatch.Signal()
