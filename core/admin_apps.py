"""Custom admin AppConfig that auto-registers every model after autodiscovery.

Wired into ``SHARED_APPS`` + ``TENANT_APPS`` in place of ``"django.contrib.admin"`` — it IS the
admin app (same ``name``/``default_site``), it just adds a post-autodiscover sweep so no model
is missing from ``/admin/``.
"""

from __future__ import annotations

from django.contrib.admin.apps import AdminConfig


class StarforgeAdminConfig(AdminConfig):
    def ready(self) -> None:
        super().ready()  # runs admin system checks + autodiscover_modules('admin')
        from core.admin_autoregister import autoregister_all

        autoregister_all()
