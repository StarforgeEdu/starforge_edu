"""Layering guard (D2-F-7 / docs/adding-an-app.md): the Day-2 domain apps are
emit-only — they must not import an sms/email/ai adapter. Notifications (D3-C) and
AI (D4-A) consume the signals; the domain stays decoupled."""

from __future__ import annotations

import re
from pathlib import Path

_APPS_ROOT = Path(__file__).resolve().parent.parent / "apps"
_DAY2_APPS = ("schedule", "attendance", "academics", "assignments", "content")
_FORBIDDEN = re.compile(r"^\s*(from|import)\s+infrastructure\.(sms|email|ai)\b", re.MULTILINE)


def test_no_external_adapter_imports_in_day2_apps():
    offenders: list[str] = []
    for app in _DAY2_APPS:
        for path in (_APPS_ROOT / app).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for match in _FORBIDDEN.finditer(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(_APPS_ROOT)}: {match.group().strip()}")
    assert offenders == [], f"Day-2 apps must stay adapter-free: {offenders}"
