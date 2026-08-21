"""Notification-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

import factory

from apps.notifications.models import (
    EventType,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationTemplate,
)
from apps.users.tests.factories import UserFactory


class NotificationFactory(factory.django.DjangoModelFactory[Notification]):
    class Meta:
        model = Notification

    user = factory.SubFactory(UserFactory)
    event_type = EventType.ATTENDANCE_ABSENT
    title = factory.Sequence(lambda n: f"Notification {n}")
    body = "body"


class NotificationDeliveryFactory(factory.django.DjangoModelFactory[NotificationDelivery]):
    class Meta:
        model = NotificationDelivery

    notification = factory.SubFactory(NotificationFactory)
    channel = NotificationDelivery._meta.get_field("channel").choices[0][0]
    status = NotificationDelivery.Status.SENT


class NotificationPreferenceFactory(factory.django.DjangoModelFactory[NotificationPreference]):
    class Meta:
        model = NotificationPreference

    user = factory.SubFactory(UserFactory)
    event_type = EventType.PAYMENTS_PAYMENT_COMPLETED
    channel = "sms"
    enabled = True


class NotificationTemplateFactory(factory.django.DjangoModelFactory[NotificationTemplate]):
    class Meta:
        model = NotificationTemplate
        django_get_or_create = ("event_type", "channel", "locale")

    event_type = EventType.ATTENDANCE_ABSENT
    channel = "in_app"
    locale = "en"
    subject = "Attendance"
    body = "Absent: $lesson_id"
