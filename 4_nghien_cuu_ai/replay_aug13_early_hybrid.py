"""Causal forensic replay for AUG13_EARLY_HYBRID_V1.

Historical outcomes are labels only. The false-adverse decision is evaluated
from the last Continuous V2 snapshot recorded at or before 20:01:05 UTC.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_CYCLES = (
    ROOT / '3_thuc_thi/quan_ly_vi_the/nhat_ky/cycles.json'
)
DEFAULT_EVENTS = Path(
    '/home/ubuntu/smc2026_data/raw/wal/bot_event/2026-08-12/20.jsonl'
)
REFERENCE_CYCLE = 'pc_1786564511599_121783'
FALSE_ADVERSE_TS = 1786564865.188


def _load_guardian():
    path = ROOT / '3_thuc_thi/ve_si_lenh/bao_ve_khan_cap.py'
    spec = importlib.util.spec_from_file_location('aug13_replay_guardian', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cycles(path):
    with Path(path).open(encoding='utf-8') as handle:
        payload = json.load(handle)
    return list(payload.get('cycles', ()))


def _nearest_long_score(path, timestamp):
    selected = None
    with Path(path).open(encoding='utf-8') as handle:
        for line in handle:
            row = json.loads(line)
            event = row.get('payload') or {}
            if event.get('event') != 'CONTINUOUS_SCORE_SHADOW':
                continue
            payload = event.get('payload') or {}
            if payload.get('analysis_type') != 'LIVE_SCORE':
                continue
            breakdown = payload.get('breakdown') or {}
            if breakdown.get('selected_bias') != 'LONG':
                continue
            ts = float(breakdown.get('snapshot_time', 0.0) or 0.0)
            if ts <= timestamp and (
                selected is None or ts > selected['snapshot_time']
            ):
                selected = breakdown
    if selected is None:
        raise RuntimeError('Không có causal LONG score trước false-adverse timestamp')
    return selected


def replay(cycles_path=DEFAULT_CYCLES, events_path=DEFAULT_EVENTS):
    rows = _cycles(cycles_path)
    reference = next(
        cycle for cycle in rows
        if cycle.get('position_cycle_id') == REFERENCE_CYCLE
    )
    entry_score = dict(reference.get('continuous_score') or {})
    causal_score = _nearest_long_score(events_path, FALSE_ADVERSE_TS)
    guardian = _load_guardian()
    false_adverse = guardian.assess_aug13_causal_exit(
        'LONG', 63420.0, 63425.0,
        {
            'adverse': ['FLASH_FLOW', 'FOOTPRINT', 'BOOK'],
            'support': [], 'status': 'SHARK_ADVERSE',
        },
        causal_score,
        {'available': True, 'realizable_edge_lcb': -0.5},
        structure_transition='NONE', structure_break_streak=0,
        age_seconds=352.0,
    )
    shadow = dict(reference.get('shadow') or {})
    holdouts = []
    for cycle in rows:
        created = float(cycle.get('created_at', 0.0) or 0.0)
        if 1786611000 <= created <= 1786612200 or 1786626900 <= created <= 1786628400:
            outcome = dict(cycle.get('shadow') or {})
            net_bps = outcome.get('net_pnl_bps')
            entry = float(outcome.get('entry_fill_price', 0.0) or 0.0)
            scaled = (
                abs(float(net_bps)) * entry * 0.001 / 10000.0
                if net_bps is not None and entry > 0.0 else None
            )
            holdouts.append({
                'position_cycle_id': cycle.get('position_cycle_id'),
                'created_at': created, 'bias': cycle.get('bias'),
                'net_bps': net_bps,
                'scaled_abs_pnl_usdt_at_0_001': scaled,
            })
    result = {
        'profile': 'AUG13_EARLY_HYBRID_V1',
        'no_lookahead': True,
        'reference_cycle': REFERENCE_CYCLE,
        'entry_claimed_before_move': bool(
            float(entry_score.get('trade_power', 0.0) or 0.0)
            >= float(entry_score.get('activation_floor', 100.0) or 100.0)
        ),
        'entry_trade_power': entry_score.get('trade_power'),
        'entry_floor': entry_score.get('activation_floor'),
        'false_adverse_snapshot_time': causal_score.get('snapshot_time'),
        'false_adverse_exit_candidate': false_adverse['candidate'],
        'false_adverse_breakdown': false_adverse,
        'terminal_label': {
            'exit_reason': shadow.get('exit_reason'),
            'net_pnl_bps': shadow.get('net_pnl_bps'),
            'mfe_bps': shadow.get('MFE_bps'),
            'mae_bps': shadow.get('MAE_bps'),
        },
        'holdouts_not_used_for_tuning': holdouts,
    }
    result['accepted'] = bool(
        result['entry_claimed_before_move']
        and not result['false_adverse_exit_candidate']
        and result['terminal_label']['exit_reason'] == 'TP2'
        and float(result['terminal_label']['net_pnl_bps'] or 0.0) > 0.0
        and all(
            row['scaled_abs_pnl_usdt_at_0_001'] is None
            or row['net_bps'] is None
            or float(row['net_bps']) >= 0.0
            or row['scaled_abs_pnl_usdt_at_0_001'] <= 0.12 + 1e-9
            for row in holdouts
        )
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cycles', type=Path, default=DEFAULT_CYCLES)
    parser.add_argument('--events', type=Path, default=DEFAULT_EVENTS)
    args = parser.parse_args()
    result = replay(args.cycles, args.events)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['accepted'] else 1)


if __name__ == '__main__':
    main()
