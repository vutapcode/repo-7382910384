"""Cycle and 15-minute regime orchestration, isolated from the live bot."""

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from datetime import datetime, timezone

from .data import build_cycle_envelope, build_regime_envelope, cycle_ready_at
from .redact import redact


def _analysis_id(config, input_hash):
    raw = f'{config.model}:{config.prompt_version}:{input_hash}'.encode('utf-8')
    return 'ai_' + hashlib.sha256(raw).hexdigest()[:24]


class ShadowWorker:
    def __init__(self, config, reader, store, client=None):
        self.config = config
        self.reader = reader
        self.store = store
        self.client = client
        self._recorder_state = None

    @staticmethod
    def _error_kind(exc):
        status = getattr(exc, 'status_code', None) or getattr(exc, 'code', None)
        text = str(exc).lower()
        if status == 429 or '429' in text or 'resource_exhausted' in text:
            return 'RATE_LIMIT'
        if status in (400, 401, 403, 404) or 'invalid api key' in text:
            return 'PERMANENT'
        return 'TRANSIENT'

    @staticmethod
    def _retry_after(exc):
        response = getattr(exc, 'response', None)
        headers = getattr(response, 'headers', {}) or {}
        try:
            value = headers.get('retry-after') or headers.get('Retry-After')
            if value is not None:
                return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
        match = re.search(r'(?:retry(?:delay| after)?)[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)s', str(exc), re.I)
        return max(0.0, float(match.group(1))) if match else 0.0

    def _recorder_fresh(self, now=None):
        now = time.time() if now is None else float(now)
        try:
            payload = json.loads(
                self.config.recorder_health_path.read_text(encoding='utf-8')
            )
        except (OSError, ValueError, TypeError):
            return False, 'RECORDER_HEALTH_MISSING'
        updated = float(payload.get('updated_at_ms', 0) or 0) / 1000.0
        if updated <= 0 or now - updated > self.config.recorder_stale_seconds:
            return False, 'RECORDER_HEALTH_STALE'
        if payload.get('current_status') == 'ERROR':
            return False, 'RECORDER_HEALTH_ERROR'
        return True, 'RECORDER_FRESH'

    async def _request(self, envelope):
        error = None
        for attempt in range(1, self.config.retries + 1):
            try:
                return await self.client.analyze(envelope)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = exc
                if self._error_kind(exc) in ('RATE_LIMIT', 'PERMANENT'):
                    break
                if attempt < self.config.retries:
                    delay = min(8.0, 2 ** (attempt - 1))
                    await asyncio.sleep(delay + random.uniform(0.0, delay * 0.25))
        raise error

    async def _process(self, envelope, input_hash, dry_run=False):
        if self.store.contains(input_hash):
            return {'input_hash': input_hash, 'result': 'DEDUPED'}
        allowed, next_retry_at = self.store.attempt_allowed(input_hash)
        if not allowed and not dry_run:
            return {
                'input_hash': input_hash, 'result': 'COOLDOWN',
                'next_retry_at': next_retry_at,
            }
        if dry_run:
            return {
                'input_hash': input_hash,
                'analysis_type': envelope['analysis_type'],
                'linkage': envelope['linkage'],
                'feature_minutes': len(envelope['market_context']['feature_minutes']),
                'bot_events': envelope['market_context']['data_quality']['bot_events_observed'],
                'result': 'DRY_RUN_READY',
            }
        if self.client is None:
            raise RuntimeError('Gemini client is unavailable')
        try:
            analysis, usage = await self._request(envelope)
        except Exception as exc:
            kind = self._error_kind(exc)
            failure = self.store.register_failure(
                input_hash, kind,
                base_seconds=self.config.failure_base_seconds,
                retry_after=self._retry_after(exc),
                circuit_threshold=self.config.circuit_threshold,
                circuit_max_seconds=self.config.circuit_max_seconds,
            )
            safe_error = redact(str(exc), secrets=(self.config.api_key,))
            self.store.health(
                'ANALYSIS_ERROR', input_hash=input_hash,
                analysis_type=envelope['analysis_type'], error_kind=kind,
                next_retry_at=failure['next_retry_at'], error=safe_error[:1000],
            )
            logging.error('[GEMINI SHADOW] analysis failed type=%s hash=%s: %s',
                          envelope['analysis_type'], input_hash[:12], safe_error)
            return {'input_hash': input_hash, 'result': 'ERROR'}

        deterministic_flags = envelope['market_context']['data_quality']['flags']
        analysis['data_quality_flags'] = list(dict.fromkeys(
            deterministic_flags + analysis['data_quality_flags']
        ))[:20]
        linkage = envelope['linkage']
        now = datetime.now(timezone.utc).isoformat()
        record = {
            'analysis_id': _analysis_id(self.config, input_hash),
            'input_hash': input_hash,
            'model': self.config.model,
            'prompt_version': self.config.prompt_version,
            'analysis_type': envelope['analysis_type'],
            'position_cycle_id': linkage.get('position_cycle_id'),
            'setup_id': linkage.get('setup_id'),
            'setup_generation': linkage.get('setup_generation'),
            'source_window': envelope['market_context']['ranges'],
            'market_regime': analysis['market_regime'],
            'summary': analysis['summary'],
            'failure_causes': analysis['failure_causes'],
            'data_quality_flags': analysis['data_quality_flags'],
            'recommendations': analysis['recommendations'],
            'supporting_evidence': analysis['supporting_evidence'],
            'contradicting_evidence': analysis['contradicting_evidence'],
            'confidence': analysis['confidence'],
            'usage': usage,
            'created_at': now,
            'authority': 'SHADOW_RESEARCH_ONLY',
        }
        written = self.store.append(record)
        if written:
            self.store.health(
                'ANALYSIS_WRITTEN', input_hash=input_hash,
                analysis_id=record['analysis_id'], analysis_type=record['analysis_type'],
            )
        return {'input_hash': input_hash, 'result': 'WRITTEN' if written else 'DEDUPED'}

    def _cycle_candidates(self, now, replay_limit=None):
        cycles = [
            cycle for cycle in self.reader.load_cycles()
            if cycle.get('status') in ('CLOSED', 'ABORTED')
            and cycle.get('position_cycle_id')
        ]
        cycles.sort(key=lambda item: float(item.get('closed_at', 0.0) or 0.0))
        if replay_limit is not None:
            limit = max(0, int(replay_limit))
            return cycles[-limit:] if limit else []
        threshold = now - self.config.cycle_lookback_hours * 3600
        recent = [
            cycle for cycle in cycles
            if float(cycle.get('closed_at', 0.0) or 0.0) >= threshold
            and cycle_ready_at(cycle, self.config.window_seconds) <= now
        ]
        return recent[-self.config.max_cycles_per_poll:]

    async def run_once(self, now=None, replay_limit=None, include_regime=True, dry_run=False):
        now = time.time() if now is None else float(now)
        if not dry_run:
            allowed, next_retry_at = self.store.global_attempt_allowed()
            if not allowed:
                return [{
                    'result': 'CIRCUIT_COOLDOWN',
                    'next_retry_at': next_retry_at,
                }]
        outcomes = []
        for cycle in self._cycle_candidates(now, replay_limit=replay_limit):
            try:
                envelope, input_hash = build_cycle_envelope(self.config, self.reader, cycle)
            except Exception as exc:
                if not dry_run:
                    self.store.health(
                        'INPUT_ERROR', position_cycle_id=cycle.get('position_cycle_id'),
                        error=redact(str(exc))[:1000],
                    )
                continue
            outcome = await self._process(envelope, input_hash, dry_run=dry_run)
            outcomes.append(outcome)
            allowed, _ = self.store.global_attempt_allowed()
            if not dry_run and not allowed:
                break

        allowed, _ = self.store.global_attempt_allowed()
        if include_regime and (dry_run or allowed):
            settled_now = now - self.config.settle_seconds
            bucket_end = (
                int(settled_now // self.config.regime_seconds)
                * self.config.regime_seconds
            )
            if bucket_end > 0:
                envelope, input_hash = build_regime_envelope(
                    self.config, self.reader, bucket_end
                )
                outcomes.append(await self._process(envelope, input_hash, dry_run=dry_run))
        return outcomes

    async def run_forever(self):
        self.store.health('WORKER_STARTED', model=self.config.model)
        while True:
            fresh, reason = self._recorder_fresh()
            if not fresh:
                if self._recorder_state != reason:
                    self.store.health('RECORDER_UNAVAILABLE', reason=reason)
                    logging.warning('[GEMINI SHADOW] paused: %s', reason)
                self._recorder_state = reason
                await asyncio.sleep(self.config.poll_seconds)
                continue
            if self._recorder_state not in (None, 'RECORDER_FRESH'):
                self.store.health('RECORDER_RECOVERED')
            self._recorder_state = 'RECORDER_FRESH'
            await self.run_once()
            await asyncio.sleep(self.config.poll_seconds)
