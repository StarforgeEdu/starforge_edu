"""Auto-register EVERY model in the Django admin.

The per-app ``admin.py`` files register the important models with tailored ModelAdmins; this
sweeps up every remaining model with a safe generic admin, so nothing is ever missing from
``/admin/`` and a newly-added model appears automatically. It runs from
``core.admin_apps.StarforgeAdminConfig.ready()`` AFTER admin autodiscovery, so it never
overrides an explicit registration.

The generic admin is built to never 500 on a large table and to be *readable*:
* every FK is a searchable **autocomplete** dropdown when the target admin supports it
  (shows the related object's name, not a bare id) — otherwise it falls back to a
  ``raw_id`` box, so we never render a huge unbounded related-object ``<select>``,
* ``list_select_related`` — no N+1 on FK columns shown in the changelist,
* ``show_full_result_count = False`` — skip the expensive full ``COUNT(*)`` on the paginator,
* ``list_display`` limited to concrete, non-binary local fields.

Autocomplete is wired in a second pass (``_promote_autocomplete``) AFTER every model is
registered, because a FK can only autocomplete to a target whose ModelAdmin already declares
``search_fields`` — which for the generic admins themselves isn't known until they all exist.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.contrib import admin
from django.db import models

_TEXT_FIELDS = (models.CharField, models.TextField, models.EmailField, models.SlugField)


def _generic_admin(model: type[models.Model]) -> type[admin.ModelAdmin]:
    concrete = list(model._meta.fields)  # concrete local fields only (no m2m / reverse)
    list_display = [f.name for f in concrete if not isinstance(f, models.BinaryField)][:8] or ["pk"]
    search = [f.name for f in concrete if isinstance(f, _TEXT_FIELDS)][:6]
    fks = [f.name for f in concrete if f.is_relation and (f.many_to_one or f.one_to_one)]

    attrs: dict[str, Any] = {
        "list_display": list_display,
        "list_per_page": 50,
        "list_select_related": True,
        "show_full_result_count": False,
        # Default every FK to raw_id; _promote_autocomplete swaps the eligible ones to
        # autocomplete once all admins (and their search_fields) are known.
        "raw_id_fields": fks,
    }
    if search:
        attrs["search_fields"] = search
    return type(f"{model.__name__}AutoAdmin", (admin.ModelAdmin,), attrs)


def _promote_autocomplete(generic_models: list[type[models.Model]]) -> None:
    """For each generic admin, turn every FK whose target admin has ``search_fields`` into a
    readable autocomplete dropdown; leave the rest as raw_id. Runs after all registration, so
    ``admin.site._registry`` is fully populated (hand + generic)."""
    for model in generic_models:
        model_admin = admin.site._registry.get(model)
        if model_admin is None:  # pragma: no cover - registration failed above
            continue
        auto: list[str] = []
        keep_raw: list[str] = []
        for fname in getattr(model_admin, "raw_id_fields", ()) or ():
            target = model._meta.get_field(fname).related_model
            target_admin = admin.site._registry.get(target) if isinstance(target, type) else None
            if target_admin is not None and getattr(target_admin, "search_fields", None):
                auto.append(fname)
            else:
                keep_raw.append(fname)
        # Each generic admin is its own unique class, so mutating the class attrs is safe.
        admin_cls = type(model_admin)
        admin_cls.autocomplete_fields = tuple(auto)
        admin_cls.raw_id_fields = tuple(keep_raw)


def autoregister_all() -> int:
    """Register every not-yet-registered, registerable model. Returns the count added."""
    added = 0
    generic_models: list[type[models.Model]] = []
    for model in apps.get_models():
        if model in admin.site._registry or getattr(model._meta, "auto_created", False):
            continue  # already registered, or an auto-created m2m through table (unregisterable)
        try:
            admin.site.register(model, _generic_admin(model))
            generic_models.append(model)
            added += 1
        except Exception:
            pass
    _promote_autocomplete(generic_models)
    return added
