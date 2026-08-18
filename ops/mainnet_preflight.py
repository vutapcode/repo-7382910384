"""Fail-closed VPS preflight for Binance Futures MAINNET shadow runtime."""
from pathlib import Path
import os
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
API_PATH = ROOT / "3_thuc_thi" / "binance_api.py"
LAUNCHER_PATH = ROOT / "mainnet_tier_s_lean_launcher.py"

def fail(message):
    print(f"[PREFLIGHT] FAIL: {message}", file=sys.stderr)
    raise SystemExit(2)

if not ENV_PATH.is_file():
    fail("missing .env; copy .env.example to .env and fill Binance API credentials")

load_dotenv(ENV_PATH, override=False)

api_key = os.getenv("BINANCE_API_KEY", "").strip()
api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
if not api_key or not api_secret:
    fail("BINANCE_API_KEY/BINANCE_API_SECRET is empty in .env")
if api_key.lower() in {"changeme", "your_key", "your_api_key"}:
    fail("BINANCE_API_KEY still contains a placeholder")
if api_secret.lower() in {"changeme", "your_secret", "your_api_secret"}:
    fail("BINANCE_API_SECRET still contains a placeholder")

if not API_PATH.is_file() or not LAUNCHER_PATH.is_file():
    fail("required mainnet runtime files are missing")

api_source = API_PATH.read_text(encoding="utf-8")
if 'https://fapi.binance.com' not in api_source:
    fail("Binance Futures MAINNET endpoint is not configured")

unsafe_flags = (
    "SMC_ENABLE_TRADING",
    "SMC_MAINNET_ARMED",
    "SMC_MAINNET_EXCLUSIVE_ACCOUNT",
)
enabled = [
    name for name in unsafe_flags
    if os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}
]
if enabled:
    fail("shadow package must stay fail-closed; enabled flags: " + ",".join(enabled))

print("[PREFLIGHT] OK: Binance Futures MAINNET credentials present; SHADOW fail-closed")
