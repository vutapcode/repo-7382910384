"""Watchdog dữ liệu và readiness gate fail-closed."""

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path


KLINE_MAX_AGE_SECONDS = max(
    5.0, float(os.getenv('SMC_KLINE_MAX_AGE_SECONDS', '8.0'))
)

FEEDS = {
    'Tick': ('thoi_gian_tick_cuoi', 3.0),
    'Orderbook': ('thoi_gian_so_lenh_cuoi', 5.0),
    'AggTrade': ('thoi_gian_dong_tien_cuoi', 5.0),
    'Kline': ('thoi_gian_nen_cuoi', KLINE_MAX_AGE_SECONDS),
    'OI/Funding': ('thoi_gian_vi_mo_cuoi', 15.0),
    'Execution price': ('execution_price_time', 3.0),
}
EVENT_LOOP_STALL_SECONDS = float(os.getenv('SMC_EVENT_LOOP_STALL_SECONDS', '3.0'))
CPU_RUNAWAY_RATIO = float(os.getenv('SMC_CPU_RUNAWAY_RATIO', '0.85'))
CPU_RUNAWAY_SECONDS = float(os.getenv('SMC_CPU_RUNAWAY_SECONDS', '5.0'))
JOURNAL_STALE_SECONDS = float(os.getenv('SMC_JOURNAL_STALE_SECONDS', '90.0'))
JOURNAL_EVENTS_PATH = Path(os.getenv(
    'SMC_JOURNAL_EVENTS_PATH',
    '/home/ubuntu/SMC2026/3_thuc_thi/quan_ly_vi_the/nhat_ky/events.jsonl',
))
RECORDER_STALE_SECONDS = float(os.getenv('SMC_RECORDER_STALE_SECONDS', '20.0'))
RECORDER_HEALTH_PATH = Path(os.getenv(
    'SMC_RECORDER_HEALTH_PATH',
    '/home/ubuntu/smc2026_data/health/status.json',
))
RECORDER_DISABLED = os.getenv('SMC_RECORDER_POLICY', 'ENABLED').upper() == 'DISABLED'


def _write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix='health_monitor_', suffix='.tmp', dir=path.parent
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def mark_recorder_stale(
    path=RECORDER_HEALTH_PATH, now_ms=None, stale_seconds=None
):
    """Persist ERROR when the recorder can no longer refresh its own health."""
    path = Path(path)
    now_ms = int(now_ms or time.time_ns() // 1_000_000)
    stale_ms = 1000.0 * float(
        RECORDER_STALE_SECONDS if stale_seconds is None else stale_seconds
    )
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return False
    updated_at = int(payload.get('updated_at_ms', 0) or 0)
    if updated_at <= 0 or now_ms - updated_at <= stale_ms:
        return False
    prior = payload.get('last_error') or {}
    if (
        payload.get('current_status') == 'ERROR'
        and prior.get('component') == 'recorder_process'
        and prior.get('observed_updated_at_ms') == updated_at
    ):
        return False
    error = {
        'component': 'recorder_process',
        'message': 'RECORDER_HEALTH_HEARTBEAT_STALE',
        'at_ms': now_ms,
        'observed_updated_at_ms': updated_at,
        'age_ms': now_ms - updated_at,
    }
    payload['status'] = 'ERROR'
    payload['current_status'] = 'ERROR'
    payload['last_error'] = error
    payload['last_error_at_ms'] = now_ms
    payload['monitor_updated_at_ms'] = now_ms
    component = payload.setdefault('component_health', {}).setdefault(
        'recorder_process', {}
    )
    component.update({
        'current_status': 'ERROR',
        'consecutive_errors': max(1, int(component.get('consecutive_errors', 0))),
        'recovered_at_ms': None,
    })
    _write_json_atomic(path, payload)
    return True


def _sample_out_of_band(state, now_mono, cpu_ratio, stall_seconds=None):
    threshold = float(
        EVENT_LOOP_STALL_SECONDS if stall_seconds is None else stall_seconds
    )
    heartbeat = float(getattr(state, 'event_loop_heartbeat_mono', 0.0) or 0.0)
    lag = max(0.0, now_mono - heartbeat) if heartbeat > 0.0 else 0.0
    stalled = heartbeat > 0.0 and lag > threshold
    state.event_loop_lag_seconds = lag
    state.process_cpu_ratio = max(0.0, float(cpu_ratio or 0.0))
    state.event_loop_stalled = stalled
    if stalled:
        state.system_ready = False
        state.trading_enabled = False
        state.last_readiness_reason = (
            f'Event loop đứng {lag:.2f}s; cpu_ratio={state.process_cpu_ratio:.2f}'
        )
    return stalled


def _sample_cpu_runaway(
    state, now_mono, cpu_ratio, hot_since=0.0,
    ratio_threshold=None, sustain_seconds=None,
):
    ratio_threshold = float(
        CPU_RUNAWAY_RATIO if ratio_threshold is None else ratio_threshold
    )
    sustain_seconds = float(
        CPU_RUNAWAY_SECONDS if sustain_seconds is None else sustain_seconds
    )
    cpu_ratio = max(0.0, float(cpu_ratio or 0.0))
    if cpu_ratio >= ratio_threshold:
        hot_since = float(hot_since or now_mono)
    else:
        hot_since = 0.0
    duration = max(0.0, now_mono - hot_since) if hot_since else 0.0
    runaway = bool(hot_since and duration >= sustain_seconds)
    state.cpu_runaway = runaway
    state.cpu_runaway_seconds = duration
    if runaway:
        state.system_ready = False
        state.trading_enabled = False
        state.last_readiness_reason = (
            f'CPU runaway {cpu_ratio:.2f} trong {duration:.1f}s'
        )
    return hot_since, runaway


def _sample_journal_health(
    state, path=JOURNAL_EVENTS_PATH, now=None, stale_seconds=None,
    now_mono=None,
):
    """Detect a dead journal without requiring idle telemetry disk writes."""
    now = float(time.time() if now is None else now)
    threshold = float(
        JOURNAL_STALE_SECONDS if stale_seconds is None else stale_seconds
    )
    observed_mono = float(
        time.monotonic() if now_mono is None else now_mono
    )
    loop_heartbeat = float(
        getattr(state, 'journal_loop_heartbeat_mono', 0.0) or 0.0
    )
    persist_heartbeat = float(
        getattr(state, 'journal_last_persist_mono', 0.0) or 0.0
    )
    if loop_heartbeat > 0.0:
        loop_age = max(0.0, observed_mono - loop_heartbeat)
        persist_age = (
            max(0.0, observed_mono - persist_heartbeat)
            if persist_heartbeat > 0.0 else loop_age
        )
        age = max(loop_age, persist_age)
    else:
        try:
            age = max(0.0, now - Path(path).stat().st_mtime)
        except OSError:
            age = float('inf')
    stalled = age > threshold
    state.journal_age_seconds = age
    state.journal_stalled = stalled
    if stalled:
        state.system_ready = False
        state.trading_enabled = False
        state.last_readiness_reason = (
            'Journal không cập nhật'
            if age == float('inf')
            else f'Journal không cập nhật {age:.1f}s'
        )
    return stalled


def start_out_of_band_watchdog(state):
    """Daemon thread quan sát starvation mà chính asyncio không thể quan sát."""
    if getattr(state, 'out_of_band_watchdog_started', False):
        return None
    state.out_of_band_watchdog_started = True
    state.event_loop_heartbeat_mono = time.monotonic()

    def monitor():
        previous_wall = time.monotonic()
        previous_cpu = time.process_time()
        alerted = set()
        cpu_hot_since = 0.0
        while True:
            time.sleep(0.5)
            now_mono = time.monotonic()
            cpu_now = time.process_time()
            elapsed = max(now_mono - previous_wall, 1e-9)
            cpu_ratio = max(0.0, (cpu_now - previous_cpu) / elapsed)
            previous_wall, previous_cpu = now_mono, cpu_now
            stalled = _sample_out_of_band(state, now_mono, cpu_ratio)
            cpu_hot_since, cpu_runaway = _sample_cpu_runaway(
                state, now_mono, cpu_ratio, cpu_hot_since
            )
            journal_stalled = _sample_journal_health(state)
            if stalled and 'event_loop' not in alerted:
                logging.critical(
                    '⛔ [OOB WATCHDOG] Event loop starvation; entry fail-closed '
                    '(lag=%.2fs cpu_ratio=%.2f).',
                    state.event_loop_lag_seconds, state.process_cpu_ratio,
                )
                alerted.add('event_loop')
            elif not stalled and 'event_loop' in alerted:
                logging.warning('✅ [OOB WATCHDOG] Event loop heartbeat đã hồi phục.')
                alerted.discard('event_loop')
            if cpu_runaway and 'cpu' not in alerted:
                logging.critical(
                    '⛔ [OOB WATCHDOG] CPU runaway; entry fail-closed '
                    '(ratio=%.2f sustained=%.1fs).',
                    state.process_cpu_ratio, state.cpu_runaway_seconds,
                )
                alerted.add('cpu')
            elif not cpu_runaway and 'cpu' in alerted:
                logging.warning('✅ [OOB WATCHDOG] CPU đã hồi phục.')
                alerted.discard('cpu')
            if journal_stalled and 'journal' not in alerted:
                logging.critical(
                    '⛔ [OOB WATCHDOG] Journal stale; entry fail-closed (age=%.1fs).',
                    state.journal_age_seconds,
                )
                alerted.add('journal')
            elif not journal_stalled and 'journal' in alerted:
                logging.warning('✅ [OOB WATCHDOG] Journal đã cập nhật lại.')
                alerted.discard('journal')
            try:
                if not RECORDER_DISABLED and mark_recorder_stale():
                    logging.error(
                        '❌ [RECORDER HEALTH] Heartbeat recorder đã stale; ghi ERROR.'
                    )
            except Exception as exc:
                logging.error(
                    '❌ [OOB WATCHDOG] Không cập nhật được recorder health: %s', exc
                )

    thread = threading.Thread(
        target=monitor, name='smc-oob-watchdog', daemon=True
    )
    thread.start()
    return thread


def _supervisor_fail_closed_reason(state):
    if getattr(state, 'supervisor_fault_latched', False):
        name = getattr(state, 'supervisor_fault_name', None)
        if name:
            return f'Supervisor fault latched: {name}'
        return 'Supervisor fault latched'
    return None


def readiness(state):
    reason = _supervisor_fail_closed_reason(state)
    if reason:
        return False, reason
    now = time.time()
    if getattr(state, 'event_loop_stalled', False):
        return False, getattr(
            state, 'last_readiness_reason', 'Event loop heartbeat bị stale'
        )
    if getattr(state, 'cpu_runaway', False):
        return False, getattr(
            state, 'last_readiness_reason', 'CPU bot bị runaway'
        )
    if getattr(state, 'journal_stalled', False):
        return False, getattr(
            state, 'last_readiness_reason', 'Journal không cập nhật'
        )
    if getattr(state, 'execution_unknown', False):
        return False, 'Có entry timeout chưa xác minh trên sàn'
    stale = []
    for label, (field, max_age) in FEEDS.items():
        timestamp = float(getattr(state, field, 0.0) or 0.0)
        if timestamp <= 0 or now - timestamp > max_age:
            stale.append(label)
    if stale:
        return False, 'Feed chưa tươi: ' + ', '.join(stale)
    if getattr(state, 'atr_1m', 0.0) <= 0 or getattr(state, 'poc', 0.0) <= 0:
        return False, 'ATR/POC chưa sẵn sàng'
    execution_bid = float(getattr(state, 'execution_best_bid', 0.0) or 0.0)
    execution_ask = float(getattr(state, 'execution_best_ask', 0.0) or 0.0)
    if execution_bid <= 0.0 or execution_ask <= execution_bid:
        return False, 'Execution bid/ask không hợp lệ'
    if not getattr(state, 'account_ready', False):
        return False, 'Chưa xác nhận balance/position mode/exchange filters'
    if not getattr(state, 'exchange_filters', {}):
        return False, 'Chưa tải exchange filters'
    if getattr(state, 'balance_usdt', 0.0) <= 0:
        return False, 'Balance khả dụng bằng 0'
    if not getattr(state, 'reconcile_ready', False):
        return False, getattr(
            state, 'last_readiness_reason', 'Reconciliation chưa xong'
        )
    return True, 'READY'


async def vong_lap_giam_sat(state):
    logging.info('🕵️ [WATCHDOG] Giám sát toàn bộ feed và readiness gate.')
    previous_reason = None
    published_ready = None
    published_reason = None

    while True:
        try:
            state.event_loop_heartbeat_mono = time.monotonic()

            # Supervisor currently drops both global flags on any worker failure.
            # Detect that external transition before recomputing readiness.  Without
            # this latch, a fresh unrelated feed can make this loop overwrite the
            # failure with READY on the next iteration.
            externally_forced_down = (
                published_ready is True
                and not bool(getattr(state, 'system_ready', False))
                and getattr(state, 'last_readiness_reason', None) == published_reason
            )
            if externally_forced_down:
                state.supervisor_fault_latched = True
                state.supervisor_fault_name = (
                    getattr(state, 'supervisor_fault_name', None) or 'worker_crash'
                )
                logging.critical(
                    '⛔ [WATCHDOG] External/supervisor failure latched; '
                    'entry remains fail-closed until process restart.'
                )

            ready, reason = readiness(state)
            state.system_ready = ready
            state.trading_enabled = (
                ready and getattr(state, 'execution_allowed', True)
            )
            state.last_readiness_reason = reason
            published_ready = ready
            published_reason = reason

            if reason != previous_reason:
                if ready:
                    if state.trading_enabled:
                        logging.info(
                            '✅ [WATCHDOG] SYSTEM READY — cho phép nhận entry.'
                        )
                    else:
                        logging.info(
                            '✅ [WATCHDOG] SYSTEM READY — DRY RUN, entry đang bị khóa.'
                        )
                else:
                    logging.warning('⛔ [WATCHDOG] Khóa entry: %s', reason)
                previous_reason = reason
        except Exception as exc:
            state.system_ready = False
            state.trading_enabled = False
            logging.exception('❌ [WATCHDOG] Lỗi readiness: %s', exc)
        await asyncio.sleep(1)
