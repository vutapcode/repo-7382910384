"""Fail-closed VPS preflight for the canonical Binance Futures MAINNET shadow runtime."""
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


# .env is optional for pure shadow. systemd supplies the canonical fail-closed flags,
# and all active market-data feeds are public. Load local overrides only when present.
if ENV_PATH.is_file():
    load_dotenv(ENV_PATH, override=False)

if not API_PATH.is_file() or not LAUNCHER_PATH.is_file():
    fail("required mainnet runtime files are missing")

api_source = API_PATH.read_text(encoding="utf-8")
if "https://fapi.binance.com" not in api_source:
    fail("Binance Futures MAINNET endpoint is not configured")

unsafe_flags = (
    "SMC_ENABLE_TRADING",
    "SMC_MAINNET_ARMED",
    "SMC_MAINNET_EXCLUSIVE_ACCOUNT",
)
enabled = [
    name
    for name in unsafe_flags
    if os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}
]
if enabled:
    fail("shadow package must stay fail-closed; enabled flags: " + ",".join(enabled))

# Credentials are not a startup dependency in canonical shadow mode. The Binance REST
# adapter may be constructed with empty credentials, while all mutation methods are
# blocked by the shadow launcher and the systemd flags above remain false.
api_key = os.getenv("BINANCE_API_KEY", "").strip()
api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
credential_state = "present" if api_key and api_secret else "not-required"

print(
    "[PREFLIGHT] OK: Binance Futures MAINNET public-data SHADOW; "
    f"credentials={credential_state}; fail-closed flags verified"
)
