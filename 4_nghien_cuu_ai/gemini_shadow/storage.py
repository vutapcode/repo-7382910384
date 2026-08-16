"""Append-only output and health storage for the shadow worker."""

import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import orjson


def _append_jsonl(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = orjson.dumps(value, option=orjson.OPT_APPEND_NEWLINE)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ShadowStore:
    def __init__(self, root):
        self.root = Path(root)
        self.input_hashes = self._load_hashes()
        self.failure_state_path = self.root / 'health' / 'failure_state.json'
        self.failure_state = self._load_failure_state()

    def _load_failure_state(self):
        try:
            value = orjson.loads(self.failure_state_path.read_bytes())
        except (OSError, orjson.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        value.setdefault('global', {'consecutive_errors': 0, 'next_retry_at': 0.0})
        value.setdefault('inputs', {})
        return value

    def _persist_failure_state(self):
        path = self.failure_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix='gemini_failure_', suffix='.tmp', dir=path.parent
        )
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(orjson.dumps(self.failure_state, option=orjson.OPT_INDENT_2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def attempt_allowed(self, input_hash, now=None):
        now = time.time() if now is None else float(now)
        global_retry = float(
            self.failure_state.get('global', {}).get('next_retry_at', 0.0) or 0.0
        )
        input_retry = float(
            self.failure_state.get('inputs', {}).get(input_hash, {}).get(
                'next_retry_at', 0.0
            ) or 0.0
        )
        next_retry = max(global_retry, input_retry)
        return now >= next_retry, next_retry

    def global_attempt_allowed(self, now=None):
        now = time.time() if now is None else float(now)
        next_retry = float(
            self.failure_state.get('global', {}).get('next_retry_at', 0.0) or 0.0
        )
        return now >= next_retry, next_retry

    def register_failure(
        self, input_hash, kind, base_seconds=60.0, retry_after=0.0,
        circuit_threshold=3, circuit_max_seconds=3600.0, now=None,
    ):
        now = time.time() if now is None else float(now)
        inputs = self.failure_state.setdefault('inputs', {})
        current = dict(inputs.get(input_hash, {}))
        attempt_count = int(current.get('attempt_count', 0) or 0) + 1
        delay = max(
            float(base_seconds), float(retry_after or 0.0),
            min(float(circuit_max_seconds), float(base_seconds) * (2 ** min(attempt_count - 1, 6))),
        )
        if kind == 'PERMANENT':
            delay = float(circuit_max_seconds)
        inputs[input_hash] = {
            'attempt_count': attempt_count,
            'last_error_kind': str(kind),
            'last_attempt_at': now,
            'next_retry_at': now + delay,
        }
        global_state = self.failure_state.setdefault('global', {})
        consecutive = int(global_state.get('consecutive_errors', 0) or 0) + 1
        global_state['consecutive_errors'] = consecutive
        global_state['last_error_kind'] = str(kind)
        global_state['last_attempt_at'] = now
        if kind == 'RATE_LIMIT':
            global_delay = min(
                float(circuit_max_seconds),
                float(base_seconds) * (2 ** min(consecutive - 1, 6)),
            )
            global_state['next_retry_at'] = now + max(
                delay, global_delay, float(retry_after or 0.0)
            )
            global_state['circuit_open'] = consecutive >= int(circuit_threshold)
        elif consecutive >= int(circuit_threshold):
            global_state['next_retry_at'] = now + min(delay, float(circuit_max_seconds))
            global_state['circuit_open'] = True
        # Bound the ledger; newest failures are the only useful retry state.
        if len(inputs) > 2048:
            oldest = sorted(
                inputs, key=lambda key: float(inputs[key].get('last_attempt_at', 0.0))
            )[:len(inputs) - 2048]
            for key in oldest:
                inputs.pop(key, None)
        self._persist_failure_state()
        return inputs[input_hash]

    def clear_failure(self, input_hash):
        self.failure_state.setdefault('inputs', {}).pop(input_hash, None)
        self.failure_state['global'] = {
            'consecutive_errors': 0, 'next_retry_at': 0.0, 'circuit_open': False,
        }
        self._persist_failure_state()

    def _load_hashes(self):
        hashes = set()
        records = self.root / 'records'
        if not records.exists():
            return hashes
        for path in records.glob('*.jsonl'):
            try:
                handle = path.open('rb')
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        record = orjson.loads(line)
                    except orjson.JSONDecodeError:
                        continue
                    value = record.get('input_hash')
                    if value:
                        hashes.add(str(value))
        return hashes

    def contains(self, input_hash):
        return input_hash in self.input_hashes

    def append(self, record):
        if self.contains(record['input_hash']):
            return False
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        _append_jsonl(self.root / 'records' / f'{day}.jsonl', record)
        self.input_hashes.add(record['input_hash'])
        self.clear_failure(record['input_hash'])
        return True

    def health(self, event, **payload):
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        _append_jsonl(self.root / 'health' / f'{day}.jsonl', {
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': str(event),
            **payload,
        })
