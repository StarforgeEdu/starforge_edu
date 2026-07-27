"""PII redaction for AI prompts (D4-LA-5, TD-11).

Before any text leaves the tenant for the Anthropic API we strip personally
identifiable information and replace each occurrence with a stable token
(``[PHONE_1]``, ``[EMAIL_1]``, ``[NATIONAL_ID_1]``, ``[STUDENT_1]`` …). The
``restore`` step is the exact inverse, so the model's output (which echoes the
tokens) can be rehydrated for storage.

Design notes:
- ``redact`` is **lossless and reversible**: ``restore(redact(t, ...)[0], map) == t``.
- The mapping is persisted on ``AIRequest.redaction_map`` (encrypted at rest via
  ``core/fields.EncryptedTextField``) — the plaintext PII never touches Redis,
  the Anthropic API, or an unencrypted column.
- Known names (the involved student/parent) are matched exactly and tokenized
  first so a name that also looks like nothing else is never leaked.
- Longest-match-first ordering on names avoids a short name shadowing a longer
  one that contains it.
"""

from __future__ import annotations

import json
import re

# Phone numbers in free-text submissions appear in many shapes, so this matches:
#   - E.164 with or without a leading + (e.g. +998901234567 / 998901234567), and
#   - grouped forms with space/dash separators (e.g. "90 123 45 67", "+998 90-123-4567").
# Deliberately broad: in free text, OVER-redaction (a stray number tokenized) is
# far safer than leaking a real phone number to the model. The grouped alternative
# requires ≥2 separated digit groups so ordinary prose ("2020 - 2024") is left alone.
_PHONE_RE = re.compile(r"\+?\d{8,15}|\+?\d{2,3}(?:[\s\-]\d{2,4}){2,4}")

# Uzbek-style national/passport id: 2 uppercase letters + 7 digits (e.g. AB1234567).
_NATIONAL_ID_RE = re.compile(r"\b[A-Z]{2}\d{7}\b")

# Pragmatic email matcher (RFC-perfect matching is not the goal — leakage is).
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


def _tokenize(
    text: str, pattern: re.Pattern[str], prefix: str, mapping: dict[str, str], counter: dict[str, int]
) -> str:
    """Replace every match of ``pattern`` with a ``[PREFIX_n]`` token, recording
    the original in ``mapping``. Identical values reuse the same token so the
    round-trip stays stable and the mapping stays compact."""
    seen: dict[str, str] = {}

    def _sub(match: re.Match[str]) -> str:
        value = match.group(0)
        token = seen.get(value)
        if token is None:
            counter[prefix] = counter.get(prefix, 0) + 1
            token = f"[{prefix}_{counter[prefix]}]"
            seen[value] = token
            mapping[token] = value
        return token

    return pattern.sub(_sub, text)


def redact(text: str, *, known_names: list[str] | None = None) -> tuple[str, dict[str, str]]:
    """Strip PII from ``text``; return ``(redacted_text, mapping)``.

    ``mapping`` maps each emitted token back to its original value. Pass it to
    ``restore`` (or persist it, encrypted) to reverse the redaction.
    """
    mapping: dict[str, str] = {}
    counter: dict[str, int] = {}
    redacted = text or ""

    # Names first (exact match, longest-first) so a name fragment can't be left
    # behind by a later regex. Skip blanks/dupes; dedupe while preserving order.
    ordered_names: list[str] = []
    for name in known_names or []:
        name = (name or "").strip()
        if name and name not in ordered_names:
            ordered_names.append(name)
    for name in sorted(ordered_names, key=len, reverse=True):
        if name in redacted:
            counter["STUDENT"] = counter.get("STUDENT", 0) + 1
            token = f"[STUDENT_{counter['STUDENT']}]"
            mapping[token] = name
            redacted = redacted.replace(name, token)

    # Then structured PII. National-id before phone/email (its pattern is the
    # most specific) — order doesn't change correctness here (disjoint shapes)
    # but keeps token numbering deterministic.
    redacted = _tokenize(redacted, _EMAIL_RE, "EMAIL", mapping, counter)
    redacted = _tokenize(redacted, _NATIONAL_ID_RE, "NATIONAL_ID", mapping, counter)
    redacted = _tokenize(redacted, _PHONE_RE, "PHONE", mapping, counter)
    return redacted, mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    """Inverse of ``redact``: substitute every token back to its original value.

    Longest token first so ``[STUDENT_1]`` is never partially clobbered by a
    prefix of ``[STUDENT_10]``.
    """
    restored = text or ""
    for token in sorted(mapping or {}, key=len, reverse=True):
        restored = restored.replace(token, mapping[token])
    return restored


def dump_map(mapping: dict[str, str]) -> str:
    """Serialize a redaction map for storage on ``AIRequest.redaction_map``."""
    return json.dumps(mapping, ensure_ascii=False)


def load_map(raw: str) -> dict[str, str]:
    """Deserialize a stored redaction map (empty/blank -> ``{}``)."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
