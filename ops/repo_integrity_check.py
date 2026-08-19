#!/usr/bin/env python3
"""Fail-closed repository + canonical runtime integrity check."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv"}
TEXT_EXTS = {".py", ".json", ".service", ".md", ".txt"}

SMOKE = r"""
import khoi_dong
import mainnet_tier_s_lean_launcher as lean

risk = lean.hardened.runtime
shadow = risk.base

required_kernel = (
    "state", "api", "main", "tai_gia_tick", "tai_dong_tien",
    "tai_coinbase", "tai_vi_mo", "tai_nen_offline", "delta_cvd", "ATR",
)
missing = [name for name in required_kernel if not hasattr(khoi_dong, name)]
if missing:
    raise RuntimeError("kernel missing: " + ",".join(missing))

if shadow.app is not khoi_dong:
    raise RuntimeError("shadow kernel identity mismatch")
for name in ("bias_council", "entry_council", "guardian_s"):
    if not hasattr(shadow, name):
        raise RuntimeError("shadow strategy missing: " + name)

if risk.base is not shadow:
    raise RuntimeError("risk launcher base identity mismatch")
for name in ("edge", "risk", "runtime_state"):
    if not hasattr(risk, name):
        raise RuntimeError("risk wiring missing: " + name)

if bool(getattr(khoi_dong.state, "execution_allowed", True)):
    raise RuntimeError("canonical state is not fail-closed")
"""


def files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_EXTS:
            yield path


def smoke_error():
    env = os.environ.copy()
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "SMC_ENABLE_TRADING": "false",
        "SMC_MAINNET_TRADING_ENABLED": "false",
        "SMC_MAINNET_ARMED": "false",
        "SMC_MAINNET_EXCLUSIVE_ACCOUNT": "false",
    })
    try:
        done = subprocess.run(
            [sys.executable, "-c", SMOKE],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "CANONICAL_IMPORT:timeout>15s"
    if done.returncode:
        detail = (done.stderr or done.stdout or "unknown import failure").strip()
        return "CANONICAL_IMPORT:" + detail[-1200:]
    return None


def main():
    errors = []
    checked = py_checked = json_checked = 0

    for path in sorted(files()):
        checked += 1
        rel = path.relative_to(ROOT)
        raw = path.read_bytes()
        if b"\x00" in raw:
            errors.append(f"{rel}: NUL_BYTE")
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

        suffix = path.suffix.lower()
        if suffix == ".py":
            py_checked += 1
            try:
                compile(text, str(rel), "exec", dont_inherit=True)
            except (SyntaxError, ValueError, TypeError) as exc:
                line = getattr(exc, "lineno", None)
                errors.append(f"{rel}:{line or ''}: PY_COMPILE:{exc}")
        elif suffix == ".json":
            json_checked += 1
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append(f"{rel}:{exc.lineno}: JSON_PARSE:{exc.msg}")

    if not errors:
        error = smoke_error()
        if error:
            errors.append(error)

    if errors:
        print("[REPO-INTEGRITY] FAIL", file=sys.stderr)
        for error in errors:
            print(" - " + error, file=sys.stderr)
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
