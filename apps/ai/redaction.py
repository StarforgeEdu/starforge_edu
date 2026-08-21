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
- Structured identifiers are tokenized before names so a first name in an
  email address cannot split the address and leak its domain.
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
_PHONE_RE = re.compile(r"(?<!\d)\+?\d{8,19}(?!\d)|(?<!\d)\+?\d{2,4}(?:[\s\-]\d{2,4}){2,5}(?!\d)")

# Uzbek-style national/passport id: 2 uppercase letters + 7 digits (e.g. AB1234567).
_NATIONAL_ID_RE = re.compile(r"\b[A-Z]{2}\d{7}\b", re.IGNORECASE)

# Pragmatic email matcher (RFC-perfect matching is not the goal — leakage is).
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_MAX_KNOWN_NAMES = 256
_MAX_KNOWN_NAME_CHARS = 512
_MAX_NAME_PATTERNS = 1024
_MAX_REDACTION_TOKENS = 1024
_MAX_REDACTION_MAP_CHARS = 128_000


def _tokenize(
    text: str, pattern: re.Pattern[str], prefix: str, mapping: dict[str, str], counter: dict[str, int]
) -> str:
    """Replace every match of ``pattern`` with a ``[PREFIX_n]`` token, recording
    the original in ``mapping``. Identical values reuse the same token so the
    round-trip stays stable and the mapping stays compact."""
    seen: dict[str, str] = {}

    def _next_token() -> str:
        while True:
            counter[prefix] = counter.get(prefix, 0) + 1
            candidate = f"[{prefix}_{counter[prefix]}]"
            # Tenant text is adversarial. Never reuse a placeholder already
            # present in the source, otherwise restoration could turn an
            # attacker-supplied token into another person's PII.
            if candidate not in text and candidate not in mapping:
                return candidate

    def _sub(match: re.Match[str]) -> str:
        value = match.group(0)
        # A prior redaction pass may have emitted this exact placeholder. Never
        # tokenize it again: nested token maps would require recursive restore
        # and could turn an attacker-controlled name into another person's PII.
        if value in mapping:
            return value
        token = seen.get(value)
        if token is None:
            if len(mapping) >= _MAX_REDACTION_TOKENS:
                raise ValueError("AI redaction map exceeds the configured token bound")
            next_map_chars = counter.get("__map_chars", 0) + len(value)
            if next_map_chars > _MAX_REDACTION_MAP_CHARS:
                raise ValueError("AI redaction map exceeds the configured size bound")
            token = _next_token()
            seen[value] = token
            mapping[token] = value
            counter["__map_chars"] = next_map_chars
        return token

    return pattern.sub(_sub, text)


def redact(text: str, *, known_names: list[str] | None = None) -> tuple[str, dict[str, str]]:
    """Strip PII from ``text``; return ``(redacted_text, mapping)``.

    ``mapping`` maps each emitted token back to its original value. Pass it to
    ``restore`` (or persist it, encrypted) to reverse the redaction.
    """
    if not isinstance(text, str):
        raise ValueError("AI redaction input must be text")
    if known_names is not None and (not isinstance(known_names, list) or len(known_names) > _MAX_KNOWN_NAMES):
        raise ValueError("AI redaction name set is outside the configured bound")

    mapping: dict[str, str] = {}
    counter: dict[str, int] = {}
    redacted = text or ""

    # Structured PII first. Email must precede names and phone numbers because
    # either can occur inside an address. National ID must precede the broad
    # numeric matcher. The shapes are replaced atomically before name matching.
    redacted = _tokenize(redacted, _EMAIL_RE, "EMAIL", mapping, counter)
    redacted = _tokenize(redacted, _NATIONAL_ID_RE, "NATIONAL_ID", mapping, counter)
    redacted = _tokenize(redacted, _PHONE_RE, "PHONE", mapping, counter)

    # Match names longest-first. Skip blanks/dupes; dedupe while preserving order.
    ordered_names: list[str] = []
    for name in known_names or []:
        if not isinstance(name, str) or len(name) > _MAX_KNOWN_NAME_CHARS:
            raise ValueError("AI redaction name is outside the configured bound")
        name = name.strip()
        if name and name not in ordered_names:
            ordered_names.append(name)
        # Free text often uses only a first or family name. Treat meaningful
        # components as PII too; over-redaction is safer than exporting one.
        for component in name.split():
            if len(component) >= 3 and component not in ordered_names:
                ordered_names.append(component)
        if len(ordered_names) > _MAX_NAME_PATTERNS:
            raise ValueError("AI redaction name patterns exceed the configured bound")
    for name in sorted(ordered_names, key=len, reverse=True):
        # Case-insensitive, Unicode word-aware matching catches spelling case
        # variation without replacing the name inside an unrelated word.
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
        redacted = _tokenize(redacted, pattern, "STUDENT", mapping, counter)

    return redacted, mapping


def _validated_mapping(mapping: object) -> dict[str, str]:
    if not isinstance(mapping, dict) or len(mapping) > _MAX_REDACTION_TOKENS:
        raise ValueError("AI redaction map is outside the configured bound")
    normalized: dict[str, str] = {}
    total_chars = 0
    for token, value in mapping.items():
        if not isinstance(token, str) or not token or not isinstance(value, str):
            raise ValueError("AI redaction map is invalid")
        total_chars += len(token) + len(value)
        if total_chars > _MAX_REDACTION_MAP_CHARS:
            raise ValueError("AI redaction map is outside the configured bound")
        normalized[token] = value
    return normalized


def restore(text: str, mapping: dict[str, str], *, max_chars: int | None = None) -> str:
    """Inverse of ``redact``: substitute every token back to its original value.

    Longest token first so ``[STUDENT_1]`` is never partially clobbered by a
    prefix of ``[STUDENT_10]``.
    """
    if not isinstance(text, str):
        raise ValueError("AI output to restore must be text")
    if isinstance(max_chars, bool) or (max_chars is not None and max_chars < 0):
        raise ValueError("AI restored-output bound is invalid")
    safe_mapping = _validated_mapping(mapping or {})
    if not safe_mapping:
        if max_chars is not None and len(text) > max_chars:
            raise ValueError("AI restored output exceeds the configured bound")
        return text

    # Perform one regex pass. Repeated ``str.replace`` is O(tokens * output) and
    # allows a provider to amplify a 250k response into expensive quadratic work.
    pattern = re.compile("|".join(re.escape(token) for token in sorted(safe_mapping, key=len, reverse=True)))
    chunks: list[str] = []
    cursor = 0
    restored_chars = 0
    for match in pattern.finditer(text):
        literal = text[cursor : match.start()]
        replacement = safe_mapping[match.group(0)]
        restored_chars += len(literal) + len(replacement)
        if max_chars is not None and restored_chars > max_chars:
            raise ValueError("AI restored output exceeds the configured bound")
        chunks.extend((literal, replacement))
        cursor = match.end()
    tail = text[cursor:]
    restored_chars += len(tail)
    if max_chars is not None and restored_chars > max_chars:
        raise ValueError("AI restored output exceeds the configured bound")
    chunks.append(tail)
    return "".join(chunks)


def dump_map(mapping: dict[str, str]) -> str:
    """Serialize a redaction map for storage on ``AIRequest.redaction_map``."""
    return json.dumps(_validated_mapping(mapping), ensure_ascii=False)


def load_map(raw: str) -> dict[str, str]:
    """Deserialize a stored redaction map (empty/blank -> ``{}``)."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    try:
        return _validated_mapping(data)
    except ValueError:
        return {}
