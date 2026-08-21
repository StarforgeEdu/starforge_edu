#!/usr/bin/env python
"""Audit user-facing strings in error paths for missing ``gettext_lazy`` (D4-LF-1).

DoD #11 / TASKS §24: every user-facing string raised from a service or serializer
error path must be translatable (wrapped in ``_()`` / ``gettext`` / ``gettext_lazy``).
This script statically scans layered service packages, legacy service modules,
serializers, and shared error modules and flags any *bare* string
literal passed as the message argument to an exception constructor.

It is intentionally narrow to avoid false positives:

* Only the FIRST positional argument of a call to a known *error* class
  (``StarforgeError`` + subclasses, DRF ``ValidationError`` / ``serializers.
  ValidationError``) is treated as a user-facing message.
* A literal is a violation only when it is "wordy" — contains a space and at
  least one alphabetic word — so machine strings like separators (``"; "``),
  codes (``"validation_error"``), and format keys are never flagged.
* Strings already wrapped in ``_( ... )`` / ``gettext( ... )`` /
  ``gettext_lazy( ... )`` are fine (the arg node is a Call, not a Constant).
* ``code=`` / ``status=`` keyword strings are stable machine codes, never
  flagged.

Exit code 0 = clean; 1 = at least one bare literal found (CI gate).

Usage::

    python scripts/check_i18n.py            # scan the default error-path set
    python scripts/check_i18n.py --json     # machine-readable findings
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Exception classes whose first positional arg is a user-facing message. Matched
# by the call's attribute/name tail, so ``serializers.ValidationError`` and a
# bare ``ValidationError`` both hit.
ERROR_CALL_NAMES = frozenset(
    {
        "StarforgeError",
        "ValidationException",
        "PermissionException",
        "NotFoundException",
        "ThrottledException",
        "ConflictException",
        "UnprocessableEntity",
        "AuthenticationException",
        "TenantContextMissing",
        "ValidationError",  # DRF + serializers.ValidationError
        "APIException",
        "PermissionDenied",
        "NotFound",
        "NotAuthenticated",
    }
)

# Names that mean "this is already translated" — a Constant wrapped in one of
# these calls is fine.
TRANSLATION_WRAPPERS = frozenset({"_", "gettext", "gettext_lazy", "ngettext", "ngettext_lazy", "pgettext"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    col: int
    call: str
    literal: str

    def render(self) -> str:
        return f"{self.path}:{self.line}:{self.col}: bare literal in {self.call}(...): {self.literal!r}"


def _call_name(func: ast.expr) -> str | None:
    """Tail name of a call target: ``serializers.ValidationError`` -> ValidationError."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_wordy_literal(value: object) -> bool:
    """A user-facing message has a space and an alpha word (filters codes/separators)."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if " " not in text:
        return False
    return any(word.isalpha() and len(word) > 1 for word in text.replace(".", " ").replace(",", " ").split())


def _literal_message_arg(call: ast.Call) -> ast.Constant | None:
    """Return the first positional arg if it is a bare wordy string Constant."""
    if not call.args:
        return None
    first = call.args[0]
    # ``"; ".join(...)`` etc. — the first arg is a Call/BinOp, not a Constant.
    if isinstance(first, ast.Constant) and _is_wordy_literal(first.value):
        return first
    return None


class ErrorPathVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[Finding] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name in ERROR_CALL_NAMES:
            literal = _literal_message_arg(node)
            if literal is not None:
                self.findings.append(
                    Finding(
                        path=self.path,
                        line=literal.lineno,
                        col=literal.col_offset,
                        call=name,
                        literal=str(literal.value),
                    )
                )
        # A translation wrapper's own Constant arg is fine — don't descend into it
        # looking for "bare" literals (it isn't bare). Descend everywhere else.
        if name in TRANSLATION_WRAPPERS:
            return
        self.generic_visit(node)


def iter_target_files() -> list[Path]:
    """Return every service/serializer module that constructs domain errors."""
    targets: set[Path] = set()
    apps_dir = REPO_ROOT / "apps"
    for pattern in (
        "*/services.py",
        "*/services/**/*.py",
        "*/serializers.py",
    ):
        targets.update(apps_dir.glob(pattern))
    for shared in (
        "core/exceptions.py",
        "core/validators.py",
    ):
        path = REPO_ROOT / shared
        if path.exists():
            targets.add(path)
    return sorted(targets)


def scan_file(path: Path) -> list[Finding]:
    rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    visitor = ErrorPathVisitor(rel)
    visitor.visit(tree)
    return visitor.findings


def run() -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_target_files():
        findings.extend(scan_file(path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit error paths for untranslated literals.")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = parser.parse_args(argv)

    findings = run()
    if args.json:
        print(json.dumps([f.__dict__ for f in findings], ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(finding.render())
        if findings:
            print(f"\n{len(findings)} bare user-facing literal(s) found. Wrap them in gettext_lazy (_()).")
        else:
            print("OK: no bare user-facing literals in serializer/service error paths.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
