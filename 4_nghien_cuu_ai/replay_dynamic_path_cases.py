#!/usr/bin/env python3
"""Causal replay of locked Dynamic Path cases from journal snapshots."""

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = (
    'pc_1786433251379_198484',  # 14:27 Vietnam
    'pc_1786446006589_332387',  # 18:00 Vietnam
)


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


path_mod = _load(
    'replay_dynamic_path',
    '2_suy_luan_mapping/tong_ket_chi_huy/dynamic_path_fee.py',
)
risk_mod = _load(
    'replay_dynamic_risk',
    '3_thuc_thi/quan_ly_vi_the/tinh_toan_rui_ro.py',
)


def _continuous_events(snapshot, score, side):
    effects = (score.get('sides', {}).get(side, {}) or {}).get(
        'evidence_effects', ()
    )
    mapping = (
        ('continuous_sweep_m1', 'SWEEP_M1', 120.0),
        ('continuous_breakout_m1', 'BREAKOUT_M1', 15.0),
        ('continuous_footprint', 'FOOTPRINT', 15.0),
        ('continuous_persistent_flow', 'PERSISTENT_FLOW', 5.0),
        ('continuous_zone_reaction', 'ZONE_REACTION', 15.0),
        ('continuous_absorption_reaction', 'ABSORPTION_REACTION', 5.0),
    )
    for field, name, ttl in mapping:
        effect = next((item for item in effects if item.get('name') == name), {})
        freshness = float(effect.get('freshness', 0.0) or 0.0)
        setattr(snapshot, field, {
            'ts': snapshot.snapshot_time - (1.0 - freshness) * ttl,
            'ttl': ttl, 'quality': float(effect.get('quality', 0.0) or 0.0),
        })


def _snapshot_from_claim(claim):
    context = dict(claim.get('context', {}) or {})
    score = dict(claim.get('score', {}) or {})
    snapshot = SimpleNamespace(**context)
    snapshot.snapshot_time = float(score.get('snapshot_time', 0.0) or 0.0)
    # Historical decision journal did not persist bounded extrema.  Do not
    # fetch future candles or invent them; current live snapshots do include it.
    snapshot.closed_m1_extrema = []
    snapshot.closed_m15_extrema = []
    persistent = context.get('persistent_flow', {}) or {}
    snapshot.price_progress_atr_3s = float(
        persistent.get('price_progress_atr_15s', 0.0) or 0.0
    ) / 5.0
    snapshot.price_progress_coverage_3s = min(
        3.0, float(persistent.get('coverage_seconds', 0.0) or 0.0)
    )
    _continuous_events(snapshot, score, str(claim.get('bias') or ''))
    return snapshot


def replay(cycles_path, events_path, cycle_ids=DEFAULT_CASES):
    saved = json.loads(Path(cycles_path).read_text(encoding='utf-8'))
    cycles = {
        cycle['position_cycle_id']: cycle
        for cycle in saved.get('cycles', ())
        if cycle.get('position_cycle_id') in set(cycle_ids)
    }
    by_setup = {cycle.get('setup_id'): cycle for cycle in cycles.values()}
    claims = {}
    with Path(events_path).open(encoding='utf-8') as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('event') != 'DECISION_EVALUATED':
                continue
            payload = event.get('payload', {}) or {}
            setup_id = payload.get('setup_id')
            if setup_id in by_setup and payload.get('result') == 'CLAIMED':
                claims[setup_id] = payload
    results = []
    for cycle_id in cycle_ids:
        cycle = cycles.get(cycle_id)
        if not cycle:
            results.append({'cycle_id': cycle_id, 'available': False, 'reason': 'CYCLE_NOT_FOUND'})
            continue
        saved_plan = dict(cycle.get('dynamic_exit_plan', {}) or {})
        if saved_plan.get('version') == path_mod.VERSION:
            plan = path_mod.reassess_saved_plan(
                saved_plan,
                float(cycle.get('requested_qty', 0.0) or 0.0),
                {'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
            )
            shadow = cycle.get('shadow', {}) or {}
            results.append({
                'cycle_id': cycle_id, 'setup_id': cycle.get('setup_id'),
                'decision_time': (cycle.get('continuous_score') or {}).get(
                    'snapshot_time'
                ),
                'future_fields_used_as_features': False,
                'saved_causal_plan_reassessed': True,
                'v2_decision': {
                    key: plan.get(key) for key in (
                        'available', 'reason', 'entry_policy',
                        'realizable_edge_lcb', 'checkpoint_monetizable',
                        'checkpoint_lock_net_bps',
                        'trailing_applies_after_tp1',
                    )
                },
                'observed_label': {
                    'shadow_net_pnl_bps': shadow.get('net_pnl_bps'),
                    'mfe_bps': shadow.get('MFE_bps'),
                    'mae_bps': shadow.get('MAE_bps'),
                    'exit_reason': shadow.get('exit_reason'),
                },
            })
            continue
        claim = claims.get(cycle.get('setup_id'))
        if not claim:
            results.append({'cycle_id': cycle_id, 'available': False, 'reason': 'CLAIM_NOT_FOUND'})
            continue
        snapshot = _snapshot_from_claim(claim)
        signal = {
            'bias': cycle.get('bias'), 'mode': cycle.get('mode'),
            'setup_kind': cycle.get('setup_kind'),
            'setup_zone': cycle.get('setup_zone'),
            'entry_style': cycle.get('entry_style'),
            'breakout_target': cycle.get('breakout_target'),
            'breakout_target2': cycle.get('breakout_target2'),
            'continuous_score': claim.get('score', {}),
        }
        entry = float(cycle.get('entry_reference_price', 0.0) or 0.0)
        levels = risk_mod.calculate_levels(
            snapshot, entry, signal['bias'], 0.1, signal['mode'],
            setup_zone=signal['setup_zone'], setup_kind=signal['setup_kind'],
        )
        plan = path_mod.plan_exit(
            snapshot, signal, float(cycle.get('requested_qty', 0.0) or 0.0),
            entry, levels['soft_sl'], 0.1,
            filters={'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
        )
        shadow = cycle.get('shadow', {}) or {}
        results.append({
            'cycle_id': cycle_id, 'setup_id': cycle.get('setup_id'),
            'decision_time': claim.get('score', {}).get('snapshot_time'),
            'future_fields_used_as_features': False,
            'historical_extrema_available': False,
            'v2_decision': {
                key: plan.get(key) for key in (
                    'available', 'reason', 'economic_pass', 'entry_policy',
                    'tp1', 'tp1_allocation', 'runner_target',
                    'net_edge_mean', 'net_edge_lcb', 'realizable_edge_lcb',
                    'checkpoint_monetizable', 'checkpoint_lock_net_bps',
                )
            },
            # Labels are attached only after the causal decision above.
            'observed_label': {
                'shadow_net_pnl_bps': shadow.get('net_pnl_bps'),
                'mfe_bps': shadow.get('MFE_bps'),
                'mae_bps': shadow.get('MAE_bps'),
                'exit_reason': shadow.get('exit_reason'),
            },
        })
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--cycles', default=ROOT / '3_thuc_thi/quan_ly_vi_the/nhat_ky/cycles.json',
        type=Path,
    )
    parser.add_argument(
        '--events', default=ROOT / '3_thuc_thi/quan_ly_vi_the/nhat_ky/events.jsonl',
        type=Path,
    )
    parser.add_argument('--cycle-id', action='append', dest='cycle_ids')
    args = parser.parse_args()
    print(json.dumps(
        replay(args.cycles, args.events, args.cycle_ids or DEFAULT_CASES),
        ensure_ascii=False, indent=2,
    ))


if __name__ == '__main__':
    main()
