"""Configuration with no trading or account credentials."""

import os
from dataclasses import dataclass
from pathlib import Path

from . import MODEL, PROMPT_VERSION


def _credential_value():
    credential_dir = os.getenv('CREDENTIALS_DIRECTORY', '')
    if credential_dir:
        path = Path(credential_dir) / 'gemini_api_key'
        try:
            return path.read_text(encoding='utf-8').strip()
        except OSError:
            pass
    return os.getenv('GEMINI_API_KEY', '').strip()


@dataclass(frozen=True)
class ShadowConfig:
    data_root: Path = Path(os.getenv('SMC_RECORDER_DATA_ROOT', '/home/ubuntu/smc2026_data'))
    cycles_path: Path = Path(os.getenv(
        'SMC_GEMINI_CYCLES_PATH',
        '/home/ubuntu/SMC2026/3_thuc_thi/quan_ly_vi_the/nhat_ky/cycles.json',
    ))
    model: str = MODEL
    prompt_version: str = PROMPT_VERSION
    symbol: str = os.getenv('SMC_GEMINI_SYMBOL', 'BTCUSDT').upper()
    poll_seconds: float = float(os.getenv('SMC_GEMINI_POLL_SECONDS', '30'))
    regime_seconds: int = int(os.getenv('SMC_GEMINI_REGIME_SECONDS', '900'))
    settle_seconds: int = int(os.getenv('SMC_GEMINI_SETTLE_SECONDS', '30'))
    window_seconds: int = int(os.getenv('SMC_GEMINI_WINDOW_SECONDS', '900'))
    cycle_lookback_hours: int = int(os.getenv('SMC_GEMINI_CYCLE_LOOKBACK_HOURS', '24'))
    max_cycles_per_poll: int = int(os.getenv('SMC_GEMINI_MAX_CYCLES_PER_POLL', '10'))
    request_timeout_seconds: float = float(os.getenv('SMC_GEMINI_TIMEOUT_SECONDS', '60'))
    retries: int = int(os.getenv('SMC_GEMINI_RETRIES', '3'))
    thinking_level: str = os.getenv('SMC_GEMINI_THINKING_LEVEL', 'low')
    failure_base_seconds: float = float(os.getenv('SMC_GEMINI_FAILURE_BASE_SECONDS', '60'))
    circuit_threshold: int = int(os.getenv('SMC_GEMINI_CIRCUIT_THRESHOLD', '3'))
    circuit_max_seconds: float = float(os.getenv('SMC_GEMINI_CIRCUIT_MAX_SECONDS', '3600'))
    recorder_stale_seconds: float = float(os.getenv('SMC_GEMINI_RECORDER_STALE_SECONDS', '15'))

    @property
    def recorder_health_path(self):
        return self.data_root / 'health' / 'status.json'

    @property
    def output_root(self):
        return self.data_root / 'derived' / 'ai_shadow'

    @property
    def api_key(self):
        return _credential_value()
