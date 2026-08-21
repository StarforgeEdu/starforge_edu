#!/usr/bin/env python
"""Generate + compile the uz/en/ru gettext catalogs (D4-LF-2).

The CI/build box for this lane has no GNU gettext toolchain (``xgettext`` /
``msgfmt`` are absent), so ``manage.py makemessages`` and ``compilemessages``
cannot run here (verified: "Can't find msguniq"). This script is the
Windows-friendly stand-in: it writes a representative ``django.po`` for each
locale and compiles it to ``django.mo`` with a pure-Python MO writer (the MO
binary format is small + stable, GNU gettext spec). On a Linux CI runner with
gettext installed, ``manage.py makemessages -a`` will extract the FULL string
set and ``compilemessages`` will compile it — this script seeds the structure +
the load-bearing translations so ``activate("uz")`` returns a real translation
today.

The msgids below are the ACTUAL user-facing strings in the codebase (copied from
services/serializers/core), so they match what gettext extracts. ``uz`` is the
project ``LANGUAGE_CODE`` (translated first), ``ru`` is best-effort, ``en`` is
the source language (identity catalog — msgstr == msgid so ``activate("en")``
is a no-op that still resolves through the catalog).

Run::

    python scripts/build_locale.py
"""

from __future__ import annotations

import struct
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = REPO_ROOT / "locale"

# msgid -> {"uz": ..., "ru": ...}. en == msgid (source language). These are real
# strings from core/exceptions.py, core/validators.py, and app serializers/
# services — extend as makemessages on CI discovers more.
CATALOG: dict[str, dict[str, str]] = {
    "Invalid input.": {
        "uz": "Noto‘g‘ri ma’lumot.",
        "ru": "Неверные данные.",
    },
    "Invalid phone number.": {
        "uz": "Telefon raqami noto‘g‘ri.",
        "ru": "Неверный номер телефона.",
    },
    "You don't have permission to do that.": {
        "uz": "Buni bajarishga ruxsatingiz yo‘q.",
        "ru": "У вас нет прав для этого действия.",
    },
    "Resource not found.": {
        "uz": "Resurs topilmadi.",
        "ru": "Ресурс не найден.",
    },
    "Forbidden.": {
        "uz": "Ruxsat etilmagan.",
        "ru": "Запрещено.",
    },
    "Too many requests.": {
        "uz": "So‘rovlar juda ko‘p.",
        "ru": "Слишком много запросов.",
    },
    "Conflict with the current state.": {
        "uz": "Joriy holat bilan ziddiyat.",
        "ru": "Конфликт с текущим состоянием.",
    },
    "The request could not be processed.": {
        "uz": "So‘rovni qayta ishlab bo‘lmadi.",
        "ru": "Запрос не может быть обработан.",
    },
    "Authentication failed.": {
        "uz": "Autentifikatsiya muvaffaqiyatsiz tugadi.",
        "ru": "Ошибка аутентификации.",
    },
    "No active tenant context.": {
        "uz": "Faol ijarachi konteksti yo‘q.",
        "ru": "Нет активного контекста арендатора.",
    },
    "Something went wrong.": {
        "uz": "Nimadir noto‘g‘ri ketdi.",
        "ru": "Что-то пошло не так.",
    },
    "Invalid username or password.": {
        "uz": "Login yoki parol noto‘g‘ri.",
        "ru": "Неверный логин или пароль.",
    },
    "Current password is incorrect.": {
        "uz": "Joriy parol noto‘g‘ri.",
        "ru": "Текущий пароль неверен.",
    },
    "Invalid code.": {
        "uz": "Kod noto‘g‘ri.",
        "ru": "Неверный код.",
    },
    "A file must be attached to a lesson or a folder.": {
        "uz": "Fayl darsga yoki papkaga biriktirilishi kerak.",
        "ru": "Файл должен быть прикреплён к уроку или папке.",
    },
    "At least one field is required.": {
        "uz": "Kamida bitta maydon talab qilinadi.",
        "ru": "Требуется хотя бы одно поле.",
    },
    "Provide plan_code and/or status.": {
        "uz": "plan_code va/yoki status ni kiriting.",
        "ru": "Укажите plan_code и/или status.",
    },
}

LANGS = ("uz", "en", "ru")

PO_HEADER = (
    'msgid ""\n'
    'msgstr ""\n'
    '"Project-Id-Version: starforge_edu\\n"\n'
    '"Report-Msgid-Bugs-To: \\n"\n'
    '"MIME-Version: 1.0\\n"\n'
    '"Content-Type: text/plain; charset=UTF-8\\n"\n'
    '"Content-Transfer-Encoding: 8bit\\n"\n'
    '"Language: {lang}\\n"\n'
    '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n'
)


def _po_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def write_po(lang: str) -> Path:
    lc_dir = LOCALE_DIR / lang / "LC_MESSAGES"
    lc_dir.mkdir(parents=True, exist_ok=True)
    po_path = lc_dir / "django.po"
    lines = [
        f"# Starforge Edu {lang} translations (D4-LF-2).",
        "# Seeded by scripts/build_locale.py; CI's `makemessages -a` extends this.",
        "#",
        PO_HEADER.format(lang=lang),
    ]
    for msgid, by_lang in CATALOG.items():
        msgstr = msgid if lang == "en" else by_lang.get(lang, "")
        lines.append(f'msgid "{_po_escape(msgid)}"')
        lines.append(f'msgstr "{_po_escape(msgstr)}"')
        lines.append("")
    po_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return po_path


def compile_mo(lang: str) -> Path:
    """Compile this lang's catalog to a GNU .mo (pure-Python; no msgfmt needed)."""
    lc_dir = LOCALE_DIR / lang / "LC_MESSAGES"
    mo_path = lc_dir / "django.mo"

    # Build the (msgid -> msgstr) map. The empty msgid carries the header.
    entries: dict[bytes, bytes] = {b"": _mo_header(lang).encode("utf-8")}
    for msgid, by_lang in CATALOG.items():
        msgstr = msgid if lang == "en" else by_lang.get(lang, "")
        if not msgstr:
            continue  # untranslated: omit so gettext falls back to the msgid
        entries[msgid.encode("utf-8")] = msgstr.encode("utf-8")

    keys = sorted(entries)  # GNU .mo requires sorted msgids
    offsets: list[tuple[int, int, int, int]] = []
    ids = b""
    strs = b""
    for key in keys:
        value = entries[key]
        offsets.append((len(ids), len(key), len(strs), len(value)))
        ids += key + b"\x00"
        strs += value + b"\x00"

    n = len(keys)
    keystart = 7 * 4 + 16 * n  # after header + the two descriptor tables
    valuestart = keystart + len(ids)
    koffsets: list[int] = []
    voffsets: list[int] = []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]

    output = struct.pack(
        "Iiiiiii",
        0x950412DE,  # GNU .mo magic
        0,  # version
        n,  # number of entries
        7 * 4,  # offset of key table
        7 * 4 + n * 8,  # offset of value table
        0,  # hash table size
        0,  # hash table offset (unused)
    )
    output += struct.pack("i" * len(koffsets), *koffsets)
    output += struct.pack("i" * len(voffsets), *voffsets)
    output += ids
    output += strs
    mo_path.write_bytes(output)
    return mo_path


def _mo_header(lang: str) -> str:
    return (
        "Project-Id-Version: starforge_edu\n"
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=UTF-8\n"
        "Content-Transfer-Encoding: 8bit\n"
        f"Language: {lang}\n"
        "Plural-Forms: nplurals=2; plural=(n != 1);\n"
    )


def main() -> int:
    for lang in LANGS:
        po = write_po(lang)
        mo = compile_mo(lang)
        print(f"{lang}: wrote {po.relative_to(REPO_ROOT)} + {mo.relative_to(REPO_ROOT)}")
    print(f"Done: {len(CATALOG)} message(s) x {len(LANGS)} locale(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
