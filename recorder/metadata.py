"""Stable identities attached to every black-box record."""

import hashlib
import os
from dataclasses import asdict
from pathlib import Path

import orjson


def _hash_bytes(chunks):
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()[:16]


def code_version(project_root=None):
    override = os.getenv('SMC_RECORDER_CODE_VERSION')
    if override:
        return override
    root = Path(project_root or Path(__file__).resolve().parents[1])
    chunks = []
    for path in sorted(root.rglob('*.py')):
        relative_parts = path.relative_to(root).parts
        if '__pycache__' in relative_parts or 'tests' in relative_parts:
            continue
        relative = path.relative_to(root).as_posix().encode()
        try:
            chunks.extend((relative, b'\0', path.read_bytes(), b'\0'))
        except OSError:
            continue
    return _hash_bytes(chunks)


def config_version(config):
    values = asdict(config)
    canonical = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in values.items()
    }
    return _hash_bytes((orjson.dumps(canonical, option=orjson.OPT_SORT_KEYS),))


def strategy_config_version():
    """Hash strategy knobs without ever persisting credentials or raw values."""
    forbidden = ('KEY', 'SECRET', 'TOKEN', 'PASS', 'PRIVATE', 'CREDENTIAL')
    dynamic = {
        # Promotion toggles these runtime state flags after the static config
        # has passed. They must not invalidate the config hash themselves.
        'SMC_ENABLE_TRADING',
        'SMC_MAINNET_ARMED',
        'SMC_MAINNET_EXCLUSIVE_ACCOUNT',
    }
    values = {
        name: value
        for name, value in os.environ.items()
        if name.startswith(('SMC_', 'WSTRADE_'))
        and not name.startswith('SMC_RECORDER_')
        and name not in dynamic
        and not any(marker in name.upper() for marker in forbidden)
    }
    return _hash_bytes((orjson.dumps(values, option=orjson.OPT_SORT_KEYS),))
