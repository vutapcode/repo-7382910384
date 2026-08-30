#!/usr/bin/env python3
"""RETIRED Whale experiment replay; never valid for Mainnet promotion.

This script remains only to inspect historical Whale/CATCH experiments. The
canonical production chain starts at `mainnet_tier_s_lean_launcher.py` and
requires a separate deterministic replay of Bias Council -> Entry Council ->
exchange independence -> regime/edge -> Guardian/Risk.
"""

import argparse
from collections import Counter
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loi_he_thong.host_cpu_governor import HostCpuGovernor
from loi_he_thong.runtime_lock import acquire_runtime_lock
from recorder.metadata import code_version
from recorder.replay import DeterministicReplay, iter_merged_records, parse_time


DEFAULT_HEARTBEAT = Path('/home/ubuntu/smc2026_data/health/bot_runtime.json')
DEFAULT_REPORT = Path('/home/ubuntu/.local/state/wstrade/replay_validation.json')
REQUIRED_WHALE_STREAMS = (
    'depth_diff', 'futures_trade_100ms', 'open_interest', 'mark_price',
    'binance_spot_trade_100ms', 'binance_spot_ticker',
    'coinbase_spot_trade_100ms',
)


def _load_source_module(name, relative_path):
    spec = spec_from_file_location(name, ROOT / relative_path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_WHALE = _load_source_module(
    'wstrade_replay_whale_intent', '2_suy_luan_mapping/whale_intent.py'
)
_WHALE_DEPTH = _load_source_module(
    'wstrade_replay_whale_depth',
    '1_tai_du_lieu/tai_whale_depth/tai_whale_depth.py',
)


class WhaleStrategyReplayAudit:
    """Run the production Whale Intent layer on recorded public events.

    The audit deliberately does not call this promotion PnL: CORE also depends
    on live Bias/Entry councils. Promotion performance still comes from closed
    shadow trades; this replay proves Whale transitions and safety invariants.
    """

    def __init__(self, metrics_start_ms=None):
        self.metrics_start_ms = metrics_start_ms
        self.engine = _WHALE.WhaleIntentEngine()
        self.state = SimpleNamespace(
            spot_cvd_buy_total=0.0, spot_cvd_sell_total=0.0,
            coinbase_cvd_buy_total=0.0, coinbase_cvd_sell_total=0.0,
            futures_cvd_buy_total=0.0, futures_cvd_sell_total=0.0,
            long_liquidation_quote_total=0.0,
            short_liquidation_quote_total=0.0,
            best_bid=0.0, best_ask=0.0, coinbase_price=0.0,
            execution_best_bid=0.0, execution_best_ask=0.0,
            open_interest=0.0, vol_pct90=0.0,
            thoi_gian_tick_cuoi=0.0,
            thoi_gian_coinbase_ticker_cuoi=0.0,
            execution_price_time=0.0,
            thoi_gian_dong_tien_cuoi=0.0,
            thoi_gian_dong_tien_futures_cuoi=0.0,
            thoi_gian_vi_mo_cuoi=0.0,
            futures_depth_updated_at=0.0,
            futures_depth_synced=False,
            futures_depth_gap_count=0,
            futures_depth_epoch=1,
            futures_depth_last_u=0,
            futures_depth_bids_top_20=[],
            futures_depth_asks_top_20=[],
            futures_depth_metrics={},
            coinbase_flow_epoch=1,
        )
        self.evaluations = 0
        self.state_samples = Counter()
        self.lane_samples = Counter()
        self.transitions = Counter()
        self.invariant_violations = Counter()
        self.last_identity = None
        self.catch_signals = 0
        self.closed_outcomes = []
        self.open_outcome = None
        self.last_futures_mid = 0.0

    @staticmethod
    def _f(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _validate_snapshot(snapshot):
        violations = []
        lane = str(snapshot.get('lane', 'NONE'))
        intent_state = str(snapshot.get('state', 'INVALID'))
        evidence = set(snapshot.get('evidence') or ())
        vetoes = tuple(snapshot.get('vetoes') or ())
        if lane == 'CATCH' and intent_state != 'RELEASE':
            violations.append('CATCH_WITHOUT_RELEASE')
        if lane == 'CATCH' and vetoes:
            violations.append('CATCH_WITH_VETO')
        if intent_state == 'RELEASE' and 'DEPTH_CONSUMPTION' not in evidence:
            violations.append('RELEASE_WITHOUT_DEPTH_CONSUMPTION')
        if lane == 'CATCH':
            floor = float(snapshot.get('flow_volume_floor_btc', 0.0) or 0.0)
            sign = 1.0 if snapshot.get('side') == 'LONG' else -1.0
            valid = 0
            for venue in ('spot', 'coinbase', 'futures'):
                row = ((snapshot.get('flow') or {}).get(venue) or {}).get('1.0') or {}
                if (
                    floor > 0.0
                    and float(row.get('volume', 0.0) or 0.0) >= floor
                    and float(row.get('imbalance', 0.0) or 0.0) * sign >= 0.08
                ):
                    valid += 1
            if valid < 2:
                violations.append('CATCH_WITHOUT_MATERIAL_FLOW_QUORUM')
        return violations

    def _mid(self):
        bid = float(self.state.execution_best_bid or 0.0)
        ask = float(self.state.execution_best_ask or 0.0)
        return (bid + ask) / 2.0 if bid > 0.0 and ask > bid else max(bid, ask)

    def _close_outcome(self, now, price, reason):
        row = self.open_outcome
        if row is None or price <= 0.0:
            return
        sign = 1.0 if row['side'] == 'LONG' else -1.0
        gross_bps = sign * (price - row['entry_price']) / row['entry_price'] * 10000.0
        row.update({
            'exit_ts': now, 'exit_price': price, 'reason': reason,
            'gross_bps': gross_bps,
            'net_bps_18': gross_bps - 18.0,
            'stress_net_bps_25': gross_bps - 25.0,
        })
        self.closed_outcomes.append(row)
        self.open_outcome = None

    def _observe(self, snapshot, now, in_metrics):
        price = self._mid()
        if price > 0.0:
            self.last_futures_mid = price
        if not in_metrics:
            return
        self.evaluations += 1
        intent_state = str(snapshot.get('state', 'INVALID'))
        lane = str(snapshot.get('lane', 'NONE'))
        side = str(snapshot.get('side', 'ABSTAIN'))
        self.state_samples[intent_state] += 1
        self.lane_samples[lane] += 1
        for violation in self._validate_snapshot(snapshot):
            self.invariant_violations[violation] += 1

        identity = (intent_state, side, lane)
        changed = identity != self.last_identity
        if changed:
            self.transitions['/'.join(identity)] += 1
            self.last_identity = identity
        if lane == 'CATCH' and changed and price > 0.0 and self.open_outcome is None:
            self.catch_signals += 1
            self.open_outcome = {
                'side': side, 'entry_ts': now, 'entry_price': price,
            }

        row = self.open_outcome
        if row is None or price <= 0.0:
            return
        sign = 1.0 if row['side'] == 'LONG' else -1.0
        move_bps = sign * (price - row['entry_price']) / row['entry_price'] * 10000.0
        if move_bps <= -55.0:
            self._close_outcome(now, price, 'HARD_STOP_55BPS')
        elif intent_state == 'EXHAUSTION' and side == row['side']:
            self._close_outcome(now, price, 'WHALE_EXHAUSTION')

    def __call__(self, record, clock):
        stream = str(record.get('stream', ''))
        payload = record.get('payload', {}) or {}
        now = float(clock.time)
        state = self.state
        if stream == 'futures_trade_100ms':
            state.futures_cvd_buy_total += self._f(payload.get('buy_qty'))
            state.futures_cvd_sell_total += self._f(payload.get('sell_qty'))
            state.thoi_gian_dong_tien_futures_cuoi = now
        elif stream == 'binance_spot_trade_100ms':
            state.spot_cvd_buy_total += self._f(payload.get('buy_qty'))
            state.spot_cvd_sell_total += self._f(payload.get('sell_qty'))
            state.thoi_gian_dong_tien_cuoi = now
        elif stream == 'coinbase_spot_trade_100ms':
            state.coinbase_cvd_buy_total += self._f(payload.get('buy_qty'))
            state.coinbase_cvd_sell_total += self._f(payload.get('sell_qty'))
            state.coinbase_price = self._f(payload.get('last_price'))
            state.thoi_gian_coinbase_ticker_cuoi = now
        elif stream == 'binance_spot_ticker':
            state.best_bid = self._f(payload.get('bid'))
            state.best_ask = self._f(payload.get('ask'))
            state.thoi_gian_tick_cuoi = now
        elif stream == 'book_ticker':
            state.execution_best_bid = self._f(payload.get('b'))
            state.execution_best_ask = self._f(payload.get('a'))
            state.execution_price_time = now
        elif stream == 'depth_diff' and payload.get('partial'):
            if _WHALE_DEPTH.apply_depth_message(state, payload, now=now):
                state.execution_best_bid = self._f(payload.get('b', [[0]])[0][0])
                state.execution_best_ask = self._f(payload.get('a', [[0]])[0][0])
                state.execution_price_time = now
        elif stream == 'open_interest':
            state.open_interest = self._f(payload.get('openInterest'))
            state.thoi_gian_vi_mo_cuoi = now
        elif stream == 'liquidation':
            order = payload.get('o', payload) or {}
            quote = self._f(order.get('z') or order.get('q')) * self._f(
                order.get('ap') or order.get('p')
            )
            if str(order.get('S', '')).upper() == 'SELL':
                state.long_liquidation_quote_total += quote
            elif str(order.get('S', '')).upper() == 'BUY':
                state.short_liquidation_quote_total += quote
        else:
            return

        snapshot = self.engine.evaluate(state, now=now)
        in_metrics = self.metrics_start_ms is None or clock.now_ms >= self.metrics_start_ms
        self._observe(snapshot, now, in_metrics)

    def summary(self):
        outcomes = list(self.closed_outcomes)
        net = [float(row['net_bps_18']) for row in outcomes]
        stress = [float(row['stress_net_bps_25']) for row in outcomes]
        gross_profit = sum(value for value in net if value > 0.0)
        gross_loss = abs(sum(value for value in net if value < 0.0))
        return {
            'scope': 'WHALE_CATCH_ONLY_NOT_PROMOTION_PNL',
            'evaluations': self.evaluations,
            'state_samples': dict(self.state_samples),
            'lane_samples': dict(self.lane_samples),
            'transitions': dict(self.transitions),
            'catch_signals': self.catch_signals,
            'closed_outcomes': len(outcomes),
            'open_outcome': self.open_outcome,
            'expectancy_net_bps_18': sum(net) / len(net) if net else None,
            'profit_factor_net_18': (
                gross_profit / gross_loss if gross_loss > 0.0
                else None if not gross_profit else float('inf')
            ),
            'stress_expectancy_bps_25': (
                sum(stress) / len(stress) if stress else None
            ),
            'invariant_violations': dict(self.invariant_violations),
        }


def whale_stream_coverage(result):
    counts = result.get('streams', {}) or {}
    required = {
        stream: max(0, int(counts.get(stream, 0) or 0))
        for stream in REQUIRED_WHALE_STREAMS
    }
    missing = [stream for stream, count in required.items() if count <= 0]
    return required, missing


def _atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='replay_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _heartbeat(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, TypeError, ValueError):
        return {}


def run(args):
    # The shared bot lock makes both real-time and accelerated replay mutually
    # exclusive with Mainnet. It does not stop or signal the bot itself.
    lock = acquire_runtime_lock('bot')
    try:
        heartbeat = _heartbeat(args.heartbeat)
        current_code = code_version(Path(__file__).resolve().parents[1])
        config = str(heartbeat.get('strategy_config_version', '') or '')
        if heartbeat.get('code_version') != current_code or not config:
            raise RuntimeError('BOT_HEARTBEAT_VERSION_MISSING_OR_STALE')
        start_ms, end_ms = parse_time(args.start), parse_time(args.end)
        read_start_ms = start_ms - max(0, int(args.warmup_seconds)) * 1000
        records = iter_merged_records(
            args.data_root, start_ms=read_start_ms, end_ms=end_ms,
        )
        strategy_audit = WhaleStrategyReplayAudit(metrics_start_ms=start_ms)
        engine = DeterministicReplay(
            metrics_start_ms=start_ms, handlers=(strategy_audit,),
        )
        governor = HostCpuGovernor()
        governor.sample()
        first_event = previous_event = None
        wall_started = time.monotonic()
        last_sample = 0.0
        cpu_peak = 0.0
        for record in records:
            event_ms = int(record.get('receive_time_ms', 0) or 0)
            if first_event is None:
                first_event = event_ms
            if previous_event is not None and args.speed > 0.0:
                delay = max(0.0, (event_ms - previous_event) / 1000.0 / args.speed)
                if delay:
                    time.sleep(delay)
            previous_event = event_ms
            engine.apply(record)
            mono = time.monotonic()
            if mono - last_sample >= 5.0:
                cpu = governor.sample(now_mono=mono)
                cpu_peak = max(
                    cpu_peak, float(cpu['host_cpu_15m_pct']),
                    float(cpu['host_cpu_1h_pct']),
                )
                if cpu['governor_mode'] in ('CONSERVE', 'DEFENSIVE', 'SAFETY_ONLY'):
                    time.sleep({'CONSERVE': .02, 'DEFENSIVE': .10, 'SAFETY_ONLY': .50}[cpu['governor_mode']])
                last_sample = mono
        result = engine.summary()
        strategy_result = strategy_audit.summary()
        required_stream_counts, missing_required_streams = whale_stream_coverage(
            result
        )
        final_cpu = governor.sample()
        governor.checkpoint()
        cpu_peak = max(
            cpu_peak, float(final_cpu['host_cpu_15m_pct']),
            float(final_cpu['host_cpu_1h_pct']),
        )
        passed = bool(
            result['records'] > 0
            and result['depth_gaps'] == 0
            and result['sequence_gap_total'] == 0
            and result['feature_cash_rows'] > 0
            and result['feature_flow_rows'] > 0
            and result['feature_flow_mismatches'] == 0
            and not missing_required_streams
            and strategy_result['evaluations'] > 0
            and not strategy_result['invariant_violations']
            and final_cpu['coverage_15m_complete']
            and final_cpu['coverage_1h_complete']
            and cpu_peak < 30.0
        )
        report = {
            'schema_version': 2, 'passed': passed,
            'strategy_authority': 'RETIRED_WHALE_EXPERIMENT_NON_AUTHORITY',
            'code_version': current_code, 'config_version': config,
            'records': result['records'], 'depth_gaps': result['depth_gaps'],
            'sequence_gaps': result['sequence_gaps'],
            'sequence_gap_total': result['sequence_gap_total'],
            'feature_cash_rows': result['feature_cash_rows'],
            'feature_flow_rows': result['feature_flow_rows'],
            'feature_flow_mismatches': result['feature_flow_mismatches'],
            'required_stream_counts': required_stream_counts,
            'missing_required_streams': missing_required_streams,
            'cash_flow': result['cash_flow'],
            'strategy_audit': strategy_result,
            'digest_sha256': result['digest_sha256'], 'speed': args.speed,
            'cpu_peak_window_pct': cpu_peak,
            'cpu_coverage_15m_seconds': final_cpu['coverage_15m_seconds'],
            'cpu_coverage_1h_seconds': final_cpu['coverage_1h_seconds'],
            'cpu_coverage_15m_complete': final_cpu['coverage_15m_complete'],
            'cpu_coverage_1h_complete': final_cpu['coverage_1h_complete'],
            'cpu_history_restored': final_cpu.get('cpu_history_restored', False),
            'event_start_ms': first_event, 'event_end_ms': previous_event,
            'elapsed_wall_seconds': time.monotonic() - wall_started,
            'updated_at_ms': int(time.time() * 1000),
        }
        _atomic(args.report, report)
        return report
    finally:
        lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', default='/home/ubuntu/smc2026_data')
    parser.add_argument('--heartbeat', default=str(DEFAULT_HEARTBEAT))
    parser.add_argument('--report', default=str(DEFAULT_REPORT))
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--speed', type=float, default=1.0)
    parser.add_argument('--warmup-seconds', type=int, default=90)
    args = parser.parse_args(argv)
    if args.speed <= 0.0:
        parser.error('--speed must be positive')
    report = run(args)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
