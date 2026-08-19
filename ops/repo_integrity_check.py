#!/usr/bin/env python3
"""Fail-closed repository source + canonical import integrity check for VPS startup."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_EXTS = {".py", ".service", ".json", ".md", ".txt"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv"}
CANONICAL_SMOKE_TIMEOUT_SECONDS = 15.0


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_EXTS:
            yield path


def canonical_import_smoke():
    script = r"""
import khoi_dong
import mainnet_tier_s_lean_launcher as lean

required_kernel = (
    "state", "api", "main", "tai_gia_tick", "tai_dong_tien",
    "tai_coinbase", "tai_vi_mo", "tai_nen_offline", "delta_cvd", "ATR",
)
missing = [name for name in required_kernel if not hasattr(khoi_dong, name)]
if missing:
    raise RuntimeError("kernel missing: " + ",".join(missing))

risk = lean.hardened.runtime
shadow = risk.base
if shadow.app is not khoi_dong:
    raise RuntimeError("shadow kernel identity mismatch")

for name in ("bias_council", "entry_council", "guardian_s"):
    if not hasattr(shadow, name):
        raise RuntimeError("shadow strategy missing: " + name)

for name in ("edge", "guardian", "runtime_state"):
    if not hasattr(risk, name):
        raise RuntimeError("risk wiring missing: " + name)

if bool(getattr(khoi_dong.state, "execution_allowed", True)):
    raise RuntimeError("canonical state is not fail-closed")
"""
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SMC_ENABLE_TRADING"] = "false"
    env["SMC_MAINNET_TRADING_ENABLED"] = "false"
    env["SMC_MAINNET_ARMED"] = "false"
    env["SMC_MAINNET_EXCLUSIVE_ACCOUNT"] = "false"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=CANONICAL_SMOKE_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown import failure").strip()
        return f"CANONICAL_IMPORT:{detail[-1200:]}"
    return None


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

    if not errors:
        try:
            smoke_error = canonical_import_smoke()
        except subprocess.TimeoutExpired:
            smoke_error = f"CANONICAL_IMPORT:timeout>{CANONICAL_SMOKE_TIMEOUT_SECONDS:.0f}s"
        except Exception as exc:
            smoke_error = f"CANONICAL_IMPORT:{type(exc).__name__}:{exc}"
        if smoke_error:
            errors.append(smoke_error)

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
        f"[REPO-INTEGRITY] OK checked={checked} python={py_checked} "
        f"json={json_checked} canonical_import=OK"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
