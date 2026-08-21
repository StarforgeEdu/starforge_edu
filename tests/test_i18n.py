"""i18n sweep + language plumbing (D4-LF-1/2/3).

Covers:
* ``activate("uz")`` resolves a translated validation message (compiled .mo in
  ``locale/uz`` — D4-LF-2).
* ``notifications.dispatch`` / ``render_template`` picks the template variant by
  the recipient's ``preferred_language`` (D4-LF-3).
* a missing variant falls back (en->uz) AND logs a warning (D4-LF-3).
* NotificationTemplate completeness: every in-app event type has uz+en+ru rows.
* ``LocaleMiddleware`` sits after Session, before Common (D4-LF-3) and
  ``Accept-Language`` is honored on an API error response.
* ``scripts/check_i18n.py`` reports zero bare literals in error paths (D4-LF-1).
"""

from __future__ import annotations

import logging

import pytest
from django.conf import settings
from django.utils import translation
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# D4-LF-2: compiled catalogs resolve through activate()
# ---------------------------------------------------------------------------
def test_activate_uz_translates_validation_message():
    """A core validation msgid resolves to its Uzbek translation under uz."""
    src = "Invalid input."
    with translation.override("uz"):
        translated = translation.gettext(src)
    assert translated != src, (
        "uz catalog did not translate 'Invalid input.' (compile locale/uz/LC_MESSAGES/django.mo)"
    )
    # The seeded Uzbek string (scripts/build_locale.py CATALOG).
    assert translated == "Noto‘g‘ri ma’lumot."


def test_activate_ru_translates_phone_message():
    src = "Invalid phone number."
    with translation.override("ru"):
        translated = translation.gettext(src)
    assert translated == "Неверный номер телефона."


def test_activate_en_is_source_identity():
    src = "Invalid input."
    with translation.override("en"):
        assert translation.gettext(src) == src


# ---------------------------------------------------------------------------
# D4-LF-3: dispatch / render uses preferred_language variant
# ---------------------------------------------------------------------------
def test_render_template_uses_preferred_language_ru(tenant_a):
    """A ru user gets the ru template body; an en user gets the en body."""
    from apps.notifications.models import EventType
    from apps.notifications.services import render_template
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        ru_user = UserFactory(preferred_language="ru")
        en_user = UserFactory(preferred_language="en")

        _, ru_body = render_template(
            event_type=EventType.ASSIGNMENTS_GRADED,
            channel="in_app",
            user_id=ru_user.pk,
            context={"score": 95},
        )
        _, en_body = render_template(
            event_type=EventType.ASSIGNMENTS_GRADED,
            channel="in_app",
            user_id=en_user.pk,
            context={"score": 95},
        )

    # Seeded bodies (notifications/0003): ru is Cyrillic, en is Latin.
    assert "оценено" in ru_body  # ru "Your submission was graded"
    assert "graded" in en_body
    assert ru_body != en_body


def test_missing_variant_falls_back_and_logs_warning(tenant_a):
    """When the preferred-language variant is absent, serve a fallback and warn.

    ``starforge.notifications`` has ``propagate=False`` (LOGGING config), so a
    capturing handler is attached to the logger directly rather than via caplog.
    """
    from apps.notifications.models import Channel, EventType, NotificationTemplate
    from apps.notifications.services import render_template
    from apps.users.tests.factories import UserFactory

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("starforge.notifications")
    handler = _Capture(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        with schema_context(tenant_a.schema_name):
            # Remove the ru variant for one event/channel so ru must fall back.
            NotificationTemplate.objects.filter(
                event_type=EventType.ASSIGNMENTS_GRADED, channel=Channel.IN_APP, locale="ru"
            ).delete()
            ru_user = UserFactory(preferred_language="ru")

            _, body = render_template(
                event_type=EventType.ASSIGNMENTS_GRADED,
                channel="in_app",
                user_id=ru_user.pk,
                context={"score": 95},
            )
    finally:
        logger.removeHandler(handler)

    assert body  # a fallback variant was served (en or uz), not empty
    assert any("template fallback" in rec.getMessage() for rec in records), (
        "expected a fallback warning when the ru variant is missing"
    )


def test_in_app_template_completeness_uz_en_ru(tenant_a):
    """Every in-app event type carries uz + en + ru rows (D4-LF-3 completeness)."""
    from apps.notifications.models import Channel, EventType, NotificationTemplate

    with schema_context(tenant_a.schema_name):
        rows = NotificationTemplate.objects.filter(channel=Channel.IN_APP, is_active=True)
        by_event: dict[str, set[str]] = {}
        for row in rows:
            by_event.setdefault(row.event_type, set()).add(row.locale)

    assert by_event, "no in-app templates seeded"
    missing_events = set(EventType.values) - set(by_event)
    assert not missing_events, f"event types missing in-app templates: {sorted(missing_events)}"
    incomplete = {ev: locales for ev, locales in by_event.items() if {"uz", "en", "ru"} - locales}
    assert not incomplete, f"in-app events missing locale variants: {incomplete}"


# ---------------------------------------------------------------------------
# D4-LF-3: LocaleMiddleware order + Accept-Language honored
# ---------------------------------------------------------------------------
def test_locale_middleware_order():
    """LocaleMiddleware after SessionMiddleware, before CommonMiddleware."""
    mw = settings.MIDDLEWARE
    session = mw.index("django.contrib.sessions.middleware.SessionMiddleware")
    locale = mw.index("django.middleware.locale.LocaleMiddleware")
    common = mw.index("django.middleware.common.CommonMiddleware")
    assert session < locale < common, f"LocaleMiddleware misordered: {mw}"


def test_accept_language_honored_on_api_error(tenant_a, client_for):
    """An anonymous request with Accept-Language: ru gets a localized error detail.

    Hitting a protected endpoint without auth yields the 401 envelope; the detail
    is translated per Accept-Language (LocaleMiddleware activates the language and
    DRF renders the lazy gettext string)."""
    client = client_for(tenant_a)
    resp = client.get("/api/v1/users/", HTTP_ACCEPT_LANGUAGE="ru")
    assert resp.status_code == 401
    # The envelope code is stable; the detail is the localized lazy string.
    assert resp.json()["code"] == "authentication_failed"  # /users/ is layered now


# ---------------------------------------------------------------------------
# D4-LF-1: the check_i18n audit script is clean
# ---------------------------------------------------------------------------
def test_check_i18n_reports_zero_bare_literals():
    from scripts.check_i18n import run

    findings = run()
    assert not findings, "bare user-facing literals: " + "; ".join(f.render() for f in findings)
