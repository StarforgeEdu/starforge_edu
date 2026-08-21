"""Operational logs must not duplicate credentials or personal communication data."""

import json
import logging
import sys
from unittest.mock import patch

from apps.auth.receivers import on_login_failed, on_otp_requested
from core.logging_filters import JsonFormatter
from core.privacy import private_fingerprint
from infrastructure.push.fcm_client import MockFCMClient
from infrastructure.sms.eskiz_client import MockEskizClient


def _logged_text(mocked_log) -> str:
    return " ".join(str(value) for call in mocked_log.call_args_list for value in (*call.args, call.kwargs))


def test_private_fingerprint_is_stable_namespaced_and_not_plaintext():
    value = "Director@Example.com"

    first = private_fingerprint(value, namespace="auth-identifier")

    assert first == private_fingerprint(value.lower(), namespace="auth-identifier")
    assert first != private_fingerprint(value, namespace="another-purpose")
    assert value.casefold() not in first


def test_auth_logs_reference_but_do_not_retain_identifiers_or_client_metadata():
    identifier = "director@example.com"
    ip = "203.0.113.44"
    user_agent = "Browser/1 secret-device-label"

    with patch("apps.auth.receivers.logger.warning") as warning:
        on_login_failed(
            object(),
            username=identifier,
            ip=ip,
            user_agent=user_agent,
            reason="wrong_password",
        )
    with patch("apps.auth.receivers.logger.info") as info:
        on_otp_requested(
            object(),
            identifier=identifier,
            purpose="password_reset",
            ip=ip,
            user_agent=user_agent,
        )

    rendered = _logged_text(warning) + _logged_text(info)
    assert identifier not in rendered
    assert ip not in rendered
    assert user_agent not in rendered
    assert "wrong_password" in rendered
    assert "password_reset" in rendered


def test_mock_provider_logs_omit_destination_token_and_message_content():
    phone = "+998901234567"
    token = "device-token-super-secret"
    title = "Student medical appointment"
    body = "Confidential message body"

    with patch("infrastructure.sms.eskiz_client.logger.info") as sms_log:
        MockEskizClient().send(phone=phone, text=body)
    with patch("infrastructure.push.fcm_client.logger.info") as push_log:
        MockFCMClient().send(token=token, title=title, body=body)

    rendered = _logged_text(sms_log) + _logged_text(push_log)
    for secret in (phone, token, title, body):
        assert secret not in rendered


def test_json_formatter_redacts_credentials_contacts_and_exception_detail():
    email = "director@example.com"
    phone = "+998 90 123 45 67"
    bearer = "session.secret-token-value"
    database_url = "postgresql://operator:database-secret@db.internal/tenant"
    try:
        raise RuntimeError(f"provider failed for {email} {phone}; password=hunter2")
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        "starforge.test",
        logging.ERROR,
        __file__,
        1,
        "Authorization: Bearer %s database=%s callback=?token=visible",
        (bearer, database_url),
        exc_info,
    )
    rendered = JsonFormatter().format(record)
    payload = json.loads(rendered)

    for sensitive in (email, phone, bearer, "database-secret", "hunter2", "token=visible"):
        assert sensitive not in rendered
    assert "[REDACTED]" in payload["msg"]
    assert "[REDACTED_EMAIL]" in payload["exc_info"]


def test_json_formatter_redacts_signed_feed_and_object_storage_credentials():
    feed_token = "eyJ1c2VyX2lkIjoxfQ:1abc:top-secret-signature"
    storage_credential = "AKIAEXAMPLE/20260802/us-east-1/s3/aws4_request"
    storage_signature = "deadbeefcafebabefeedface"
    google_credential = "service@example.iam.gserviceaccount.com/20260802/auto/storage/goog4_request"
    google_signature = "0123456789abcdef"
    message = (
        f"failed /api/v1/schedule/ical/{feed_token}/ then "
        f"https://storage.example/object?X-Amz-Credential={storage_credential}"
        f"&X-Amz-Signature={storage_signature}&X-Amz-Security-Token=session-token and "
        f"https://storage.example/other?X-Goog-Credential={google_credential}"
        f"&X-Goog-Signature={google_signature}"
    )
    record = logging.LogRecord("starforge.test", logging.ERROR, __file__, 1, message, (), None)

    rendered = JsonFormatter().format(record)

    for sensitive in (
        feed_token,
        storage_credential,
        storage_signature,
        "session-token",
        google_credential,
        google_signature,
    ):
        assert sensitive not in rendered
    assert "/api/v1/schedule/ical/[REDACTED_FEED_TOKEN]/" in rendered
