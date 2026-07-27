"""Template filters for report PDF rendering.

``dictkey`` looks up ``mapping[key]`` where ``key`` is a template variable —
Django's dotted-path syntax can't index a dict by a runtime column name, so the
report tables (generic columns/rows) need this.
"""

from __future__ import annotations

from typing import Any

from django import template

register = template.Library()


@register.filter
def dictkey(mapping: Any, key: Any) -> Any:
    """Return ``mapping[key]`` (blank when absent / not a mapping). Booleans
    render as their str form so a False cell is visible, not blank."""
    try:
        value = mapping.get(key, "")
    except AttributeError:
        return ""
    return value
