#!/usr/bin/env python3
"""Fail-closed repository text/source integrity check for VPS startup."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_EXTS = {".py", ".service", ".json", ".md", ".txt"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv"}


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_EXTS:
            yield path


def main() -> int:
    errors = []
    checked = 0
    py_checked = 0
    json_checked = 0

    for path in sorted(iter_files()):
        checked += 1
        rel = path.relative_to(ROOT)
        raw = path.read_bytes()

        if b"\x00" in raw:
            errors.append(f"{rel}: NULL_BYTE")
            continue
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            errors.append(f"{rel}: UTF16_BOM")
            continue
        if raw.startswith(b"\xef\xbb\xbf"):
            errors.append(f"{rel}: UTF8_BOM")
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{rel}: INVALID_UTF8:{exc.start}")
            continue

        if path.suffix.lower() == ".py":
            py_checked += 1
            try:
                compile(text, str(rel), "exec", dont_inherit=True)
            except (SyntaxError, ValueError, TypeError) as exc:
                line = getattr(exc, "lineno", None)
                where = f":{line}" if line else ""
                errors.append(f"{rel}{where}: PY_COMPILE:{exc}")
        elif path.suffix.lower() == ".json":
            json_checked += 1
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append(f"{rel}:{exc.lineno}: JSON_PARSE:{exc.msg}")

    if errors:
        print("[REPO-INTEGRITY] FAIL", file=sys.stderr)
        for item in errors:
            print(f" - {item}", file=sys.stderr)
        print(
            f"[REPO-INTEGRITY] checked={checked} python={py_checked} "
            f"json={json_checked} errors={len(errors)}",
            file=sys.stderr,
        )
        return 1

    print(
        f"[REPO-INTEGRITY] OK checked={checked} python={py_checked} json={json_checked}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
