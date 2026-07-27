"""Every model is registered in the Django admin (core.admin_apps auto-registers the rest),
and every registration is valid (passes Django's admin system checks)."""

from __future__ import annotations

import pytest
from django.apps import apps
from django.contrib import admin

pytestmark = pytest.mark.django_db


def test_every_model_is_registered_in_admin():
    missing = [
        f"{m._meta.app_label}.{m.__name__}"
        for m in apps.get_models()
        if m not in admin.site._registry and not getattr(m._meta, "auto_created", False)
    ]
    assert missing == [], f"models missing from /admin/: {missing}"


def test_all_admin_registrations_pass_system_checks():
    """Django's admin checks validate every registered ModelAdmin (list_display / raw_id_fields
    / search_fields reference real fields, etc.) — the auto-registered admins must be valid so
    the changelist/form never 500s."""
    serious = [msg for msg in admin.site.check(None) if msg.is_serious()]
    assert not serious, [str(m) for m in serious]
