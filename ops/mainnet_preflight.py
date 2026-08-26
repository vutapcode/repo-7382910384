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


# .env is optional for pure shado. systemd supplies the canonical fail-closed flags,
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
    "SMC_MAINNET_TRADING_ENABLED",
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

mode = os.getenv("WSTRADE_MODE", "SHADOW").strip().upper()
credential_dir = Path(os.getenv("CREDENTIALS_DIRECTORY", ""))
key_path = credential_dir / "binance_api_key"
secret_path = credential_dir / "binance_api_secret"

def credential_present(path):
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False

key_present = credential_present(key_path)
secret_present = credential_present(secret_path)
if key_present != secret_present:
    fail("partial Binance systemd credentials; provide both key and secret")
if mode == "DIRECT_LIVE" and not (key_present and secret_present):
    fail("DIRECT_LIVE requires both Binance systemd credentials")

credential_state = "present" if key_present and secret_present else "not-required"
print(
    f"[PREFLIGHT] OK: mode={mode}; credentials={credential_state}; "
    "startup flags fail-closed"
)
