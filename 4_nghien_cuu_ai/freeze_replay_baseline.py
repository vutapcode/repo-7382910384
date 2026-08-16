"""Freeze a deterministic, read-only replay baseline for strategy changes.

The tool never imports Executor and never calls an exchange API.  It copies the
recorded receive-order WAL for bounded case windows, extracts the matching bot
journal, and fingerprints the trading code/config that produced the decisions.
Existing baseline directories are immutable: a second run must use a new ID.
"""

import argparse
import gzip
import hashlib
import heapq
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import orjson

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from recorder.replay import DEFAULT_STREAMS


DEFAULT_DATA_ROOT = Path('/home/ubuntu/smc2026_data')
DEFAULT_OUTPUT_ROOT = DEFAULT_DATA_ROOT / 'derived' / 'replay_baselines'
ROUND_TRIP_COST_BPS = 8.0
CAPTURE_RATIO = 0.60


DEFAULT_CASES = (
    {
        'case_id': 'miss_long_0710_vn',
        'kind': 'MISSED_OPPORTUNITY',
        'expected_direction': 'LONG',
        'start_ms': 1786320000000,
        'end_ms': 1786320720000,
        'focus_ms': 1786320632999,
        'counterfactual_target': 65198.713,
        'annotation': 'FAILED_BREAK_RECLAIM_65043_5',
    },
    {
        'case_id': 'miss_long_0847_vn',
        'kind': 'MISSED_OPPORTUNITY',
        'expected_direction': 'LONG',
        'start_ms': 1786325880000,
        'end_ms': 1786326660000,
        'focus_ms': 1786326451999,
        'counterfactual_target': 65300.0,
        'annotation': 'FAILED_BREAK_RECLAIM_65043_5',
    },
    {
        'case_id': 'win_short_0851_vn',
        'kind': 'EXECUTED_WIN',
        'expected_direction': 'SHORT',
        'start_ms': 1786326480000,
        'end_ms': 1786327920000,
        'focus_ms': 1786326688325,
        'position_cycle_id': 'pc_1786326688324_954516',
        'annotation': 'SWEEP_65300_REVERSAL',
    },
    {
        'case_id': 'loss_short_0121_utc',
        'kind': 'EXECUTED_LOSS',
        'expected_direction': 'SHORT',
        'start_ms': 1786324560000,
        'end_ms': 1786325760000,
        'focus_ms': 1786324867918,
        'position_cycle_id': 'pc_1786324867917_397068',
        'annotation': 'RECENT_TRANSITION_PULLBACK_LOSS',
    },
    {
        'case_id': 'loss_long_2240_utc',
        'kind': 'EXECUTED_LOSS',
        'expected_direction': 'LONG',
        'start_ms': 1786314900000,
        'end_ms': 1786318200000,
        'focus_ms': 1786315203167,
        'position_cycle_id': 'pc_1786315203166_767922',
        'annotation': 'RECENT_NEUTRAL_FADE_LOSS',
    },
)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def utc_iso(ms):
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).isoformat()


def vietnam_iso(ms):
    # Fixed UTC+7 is intentional; this dataset is historical and has no DST.
    vietnam = timezone(timedelta(hours=7))
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).astimezone(
        vietnam
    ).isoformat()


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_cycles(path):
    document = json.loads(Path(path).read_text())
    return document, {
        item['position_cycle_id']: item for item in document.get('cycles', [])
    }


def _hour_partitions(start_ms, end_ms):
    cursor = int(start_ms // 3_600_000) * 3_600_000
    final = int(end_ms // 3_600_000) * 3_600_000
    while cursor <= final:
        moment = datetime.fromtimestamp(cursor / 1000.0, timezone.utc)
        yield moment.strftime('%Y-%m-%d'), moment.strftime('%H')
        cursor += 3_600_000


def _iter_window_file(path, start_ms, end_ms):
    with Path(path).open('rb') as handle:
        for line in handle:
            if not line.strip():
                continue
            record = orjson.loads(line)
            receive_ms = int(record.get('receive_time_ms', 0) or 0)
            if start_ms <= receive_ms <= end_ms:
                yield record


def iter_window_records(data_root, streams, start_ms, end_ms):
    """Merge only UTC hour partitions intersecting the requested window."""
    paths = []
    wal_root = Path(data_root) / 'raw' / 'wal'
    partitions = tuple(_hour_partitions(start_ms, end_ms))
    for stream in sorted(set(streams)):
        for day, hour in partitions:
            path = wal_root / stream / day / f'{hour}.jsonl'
            if path.exists():
                paths.append(path)
    iterators, heap = [], []
    for index, path in enumerate(paths):
        iterator = iter(_iter_window_file(path, start_ms, end_ms))
        iterators.append(iterator)
        try:
            record = next(iterator)
        except StopIteration:
            continue
        key = (
            int(record.get('receive_time_ms', 0) or 0),
            int(record.get('event_time_ms', 0) or 0),
            str(record.get('stream', '')),
            index,
        )
        heapq.heappush(heap, (key, index, record))
    while heap:
        _, index, record = heapq.heappop(heap)
        yield record
        try:
            following = next(iterators[index])
        except StopIteration:
            continue
        key = (
            int(following.get('receive_time_ms', 0) or 0),
            int(following.get('event_time_ms', 0) or 0),
            str(following.get('stream', '')),
            index,
        )
        heapq.heappush(heap, (key, index, following))


def iter_journal(path, start_ms, end_ms):
    start_s, end_s = start_ms / 1000.0, end_ms / 1000.0
    with Path(path).open() as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            ts = float(item.get('ts', 0.0) or 0.0)
            if ts > end_s:
                break
            if start_s <= ts:
                yield item


def feature_rows(data_root, start_ms, end_ms):
    rows = []
    for record in iter_window_records(
        data_root, streams={'feature_1s'}, start_ms=start_ms, end_ms=end_ms,
    ):
        payload = record.get('payload', {}) or {}
        mid = {
            key: _number(payload.get(key))
            for key in ('mid_open', 'mid_high', 'mid_low', 'mid_close')
        }
        rows.append({
            'event_time_ms': int(record.get('event_time_ms', 0) or 0),
            'receive_time_ms': int(record.get('receive_time_ms', 0) or 0),
            **mid,
            'buy_qty': _number(payload.get('buy_qty')) or 0.0,
            'sell_qty': _number(payload.get('sell_qty')) or 0.0,
            'spread_bps_max': _number(payload.get('spread_bps_max')),
            'event_counts': payload.get('event_counts', {}),
            'macro': payload.get('macro', {}),
        })
    return rows


def market_summary(rows, focus_ms, direction):
    valid = [
        row for row in rows
        if all(row.get(key) is not None for key in (
            'mid_open', 'mid_high', 'mid_low', 'mid_close'
        ))
    ]
    if not valid:
        return {'rows': len(rows), 'valid_price_rows': 0}
    focus = min(valid, key=lambda row: abs(row['event_time_ms'] - focus_ms))
    entry = float(focus['mid_close'])
    sign = 1.0 if direction == 'LONG' else -1.0
    future = [row for row in valid if row['event_time_ms'] >= focus['event_time_ms']]
    favorable = [row['mid_high'] if sign > 0 else row['mid_low'] for row in future]
    adverse = [row['mid_low'] if sign > 0 else row['mid_high'] for row in future]
    return {
        'rows': len(rows),
        'valid_price_rows': len(valid),
        'unique_feature_seconds': len({row['event_time_ms'] // 1000 for row in rows}),
        'open': valid[0]['mid_open'],
        'high': max(row['mid_high'] for row in valid),
        'low': min(row['mid_low'] for row in valid),
        'close': valid[-1]['mid_close'],
        'buy_qty': sum(row['buy_qty'] for row in rows),
        'sell_qty': sum(row['sell_qty'] for row in rows),
        'focus_price': entry,
        'focus_event_time_ms': focus['event_time_ms'],
        'directional_mfe_bps': max(
            sign * (price - entry) / entry * 10000.0 for price in favorable
        ),
        'directional_mae_bps': min(
            sign * (price - entry) / entry * 10000.0 for price in adverse
        ),
        'directional_close_bps': sign * (
            valid[-1]['mid_close'] - entry
        ) / entry * 10000.0,
        'directional_net_mfe_bps_after_8bps': max(
            sign * (price - entry) / entry * 10000.0 for price in favorable
        ) - ROUND_TRIP_COST_BPS,
    }


def context_projection(event):
    payload = event.get('payload', {}) or {}
    context = payload.get('context', {}) or {}
    score = payload.get('score', {}) or {}
    return {
        'ts': event.get('ts'),
        'setup_id': payload.get('setup_id'),
        'mode': payload.get('mode'),
        'bias': payload.get('bias'),
        'decision_result': payload.get('result'),
        'candidate_created': payload.get('setup_id') is not None,
        'm15_state': {
            'trend': context.get('trend_m15'),
            'transition': context.get('structure_transition'),
            'broken_level': context.get('structure_broken_level'),
            'break_streak': context.get('structure_break_streak'),
            'swing_high': context.get('swing_high_m15'),
            'swing_low': context.get('swing_low_m15'),
        },
        'm1_structure': {
            'breakout': context.get('breakout_m1'),
            'sweep': context.get('sweep_m1'),
        },
        'zone': {
            'poc': context.get('poc'),
            'vah': context.get('vah'),
            'val': context.get('val'),
            'reaction': context.get('zone_reaction'),
            'acceptance_trap': context.get('zone_acceptance_trap'),
        },
        'flow': {
            'volume_3s': context.get('current_vol_3s'),
            'volume_p90': context.get('vol_pct90'),
            'buy_3s': context.get('current_cvd_buy_3s'),
            'sell_3s': context.get('current_cvd_sell_3s'),
            'buy_30m': context.get('cvd_buy_30m'),
            'sell_30m': context.get('cvd_sell_30m'),
            'persistent': context.get('persistent_flow'),
            'price_trap': context.get('flow_price_trap'),
        },
        'footprint': context.get('fp_last_imbalance'),
        'absorption_reaction': context.get('absorption_reaction'),
        'veto_reason': payload.get('veto_reason'),
        'core': score.get('core'),
        'shark': score.get('shark'),
        'score_detail': score.get('detail', []),
        'event_ids': score.get('event_ids', []),
        'advisory': score.get('advisory', {}),
    }


def journal_summary(events, case, cycle):
    decisions = [item for item in events if item.get('event') == 'DECISION_EVALUATED']
    nearest = min(
        decisions,
        key=lambda item: abs(float(item.get('ts', 0.0)) * 1000 - case['focus_ms']),
    ) if decisions else None
    result_counts = Counter(
        str((item.get('payload', {}) or {}).get('result', 'UNKNOWN'))
        for item in decisions
    )
    armed = [
        item for item in events if item.get('event') == 'RADAR_ARMED_WINDOW'
    ]
    candidate_directions = Counter(
        str((item.get('payload', {}) or {}).get('bias', 'UNKNOWN'))
        for item in armed
    )
    economics = []
    for item in events:
        if item.get('event') != 'ECONOMIC_GATE_EVALUATED':
            continue
        payload = item.get('payload', {}) or {}
        economic = payload.get('economic', {}) or {}
        economics.append({
            'ts': item.get('ts'),
            'position_cycle_id': item.get('position_cycle_id'),
            'setup_id': payload.get('setup_id'),
            'result': payload.get('result'),
            'target_basis': economic.get('target_basis'),
            'tp1_distance_bps': economic.get('tp1_distance_bps'),
            'projected_capture_bps': economic.get('projected_capture_bps'),
            'all_in_cost_bps': economic.get('all_in_cost_bps'),
            'expected_net_edge_bps': economic.get('expected_net_edge_bps'),
            'reason': economic.get('reason'),
        })
    final_decision = (
        'NO_EXPECTED_DIRECTION_CANDIDATE'
        if candidate_directions.get(case['expected_direction'], 0) == 0
        else 'EXPECTED_DIRECTION_CANDIDATE_CREATED'
    )
    if cycle:
        final_decision = cycle.get('status')
    return {
        'events': len(events),
        'event_counts': dict(Counter(item.get('event', 'UNKNOWN') for item in events)),
        'decision_result_counts': dict(result_counts),
        'armed_candidate_directions': dict(candidate_directions),
        'expected_direction_candidate_created': bool(
            candidate_directions.get(case['expected_direction'], 0)
        ),
        'nearest_focus_decision': context_projection(nearest) if nearest else None,
        'economic_evaluations': economics,
        'final_decision': final_decision,
    }


def counterfactual_annotation(case, market):
    target = _number(case.get('counterfactual_target'))
    entry = _number(market.get('focus_price'))
    if not target or not entry:
        return {
            'evaluated_by_live_bot': False,
            'reason': 'NO_COUNTERFACTUAL_TARGET',
        }
    direction = case['expected_direction']
    raw = (
        target - entry if direction == 'LONG' else entry - target
    ) / entry * 10000.0
    projected = raw * CAPTURE_RATIO
    return {
        'evaluated_by_live_bot': False,
        'label': 'RESEARCH_ONLY_DO_NOT_TREAT_AS_LIVE_DECISION',
        'target': target,
        'raw_target_bps': raw,
        'capture_ratio': CAPTURE_RATIO,
        'projected_capture_bps': projected,
        'assumed_all_in_cost_bps': ROUND_TRIP_COST_BPS,
        'projected_net_edge_bps': projected - ROUND_TRIP_COST_BPS,
    }


def code_manifest(project_root):
    roots = [
        '1_tai_du_lieu', '2_suy_luan_mapping', '3_thuc_thi',
        'khoi_dong.py', 'bo_nho_ram.py',
    ]
    files = []
    for relative in roots:
        path = project_root / relative
        if path.is_file():
            candidates = [path]
        elif path.exists():
            candidates = sorted(path.rglob('*.py'))
        else:
            candidates = []
        for candidate in candidates:
            files.append({
                'path': str(candidate.relative_to(project_root)),
                'size': candidate.stat().st_size,
                'sha256': sha256_file(candidate),
            })
    return files


def secret_manifest():
    candidates = [
        PROJECT_ROOT / '.env',
        Path('/home/ubuntu/.config/smc2026/bot.env'),
        Path('/home/ubuntu/.config/smc2026/gemini-shadow.env'),
    ]
    result = []
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        keys = []
        for line in path.read_text(errors='replace').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            keys.append(stripped.split('=', 1)[0].strip())
        result.append({
            'path': str(path),
            'sha256': sha256_file(path),
            'variable_names': sorted(set(keys)),
            'values_redacted': True,
        })
    return result


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    )


def write_replay_input(path, data_root, start_ms, end_ms, warmup_seconds):
    records = 0
    streams = Counter()
    read_start = start_ms - max(0, warmup_seconds) * 1000
    with Path(path).open('wb') as raw_handle, gzip.GzipFile(
        filename='', mode='wb', fileobj=raw_handle, mtime=0
    ) as handle:
        for record in iter_window_records(
            data_root, streams=DEFAULT_STREAMS,
            start_ms=read_start, end_ms=end_ms,
        ):
            line = orjson.dumps(record, option=orjson.OPT_SORT_KEYS) + b'\n'
            handle.write(line)
            records += 1
            streams[str(record.get('stream', 'UNKNOWN'))] += 1
    return {
        'path': Path(path).name,
        'warmup_seconds': warmup_seconds,
        'records': records,
        'streams': dict(streams),
        'compression': 'gzip_mtime_zero',
        'sha256': sha256_file(path),
        'size_bytes': Path(path).stat().st_size,
    }


def freeze(args):
    output = Path(args.output_root) / args.baseline_id
    if output.exists():
        raise FileExistsError(f'IMMUTABLE_BASELINE_EXISTS: {output}')
    staging = Path(args.output_root) / f'.{args.baseline_id}.tmp.{os.getpid()}'
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    journal_path = PROJECT_ROOT / '3_thuc_thi/quan_ly_vi_the/nhat_ky/events.jsonl'
    cycles_path = PROJECT_ROOT / '3_thuc_thi/quan_ly_vi_the/nhat_ky/cycles.json'
    cycles_document, cycles_by_id = load_cycles(cycles_path)

    cycle_catalog = []
    for cycle in cycles_document.get('cycles', []):
        shadow = cycle.get('shadow', {}) or {}
        if shadow.get('shadow_kind') != 'EXECUTED' or shadow.get('status') != 'CLOSED':
            continue
        pnl = _number(shadow.get('net_pnl_bps'))
        cycle_catalog.append({
            'position_cycle_id': cycle.get('position_cycle_id'),
            'setup_id': cycle.get('setup_id'),
            'mode': cycle.get('mode'),
            'bias': cycle.get('bias'),
            'created_at': cycle.get('created_at'),
            'classification': 'WIN' if pnl is not None and pnl > 0 else 'LOSS',
            'net_pnl_bps': pnl,
            'MFE_bps': shadow.get('MFE_bps'),
            'MAE_bps': shadow.get('MAE_bps'),
            'exit_reason': shadow.get('exit_reason'),
        })
    write_json(staging / 'executed_cycle_catalog.json', cycle_catalog)

    case_manifests = []
    for case in DEFAULT_CASES:
        case_dir = staging / case['case_id']
        case_dir.mkdir()
        events = list(iter_journal(journal_path, case['start_ms'], case['end_ms']))
        trace_path = case_dir / 'decision_trace.jsonl.gz'
        with trace_path.open('wb') as raw_handle, gzip.GzipFile(
            filename='', mode='wb', fileobj=raw_handle, mtime=0
        ) as handle:
            for item in events:
                handle.write(
                    orjson.dumps(item, option=orjson.OPT_SORT_KEYS) + b'\n'
                )
        rows = feature_rows(args.data_root, case['start_ms'], case['end_ms'])
        market = market_summary(rows, case['focus_ms'], case['expected_direction'])
        cycle = cycles_by_id.get(case.get('position_cycle_id'))
        summary = {
            **case,
            'start_utc': utc_iso(case['start_ms']),
            'end_utc': utc_iso(case['end_ms']),
            'focus_utc': utc_iso(case['focus_ms']),
            'focus_vietnam': vietnam_iso(case['focus_ms']),
            'market': market,
            'decision_funnel': journal_summary(events, case, cycle),
            'counterfactual_economics': counterfactual_annotation(case, market),
            'cycle': cycle,
        }
        write_json(case_dir / 'case.json', summary)
        replay_input = write_replay_input(
            case_dir / 'replay_input.jsonl.gz', args.data_root,
            case['start_ms'], case['end_ms'], args.warmup_seconds,
        )
        artifacts = {
            'case_json_sha256': sha256_file(case_dir / 'case.json'),
            'decision_trace_path': trace_path.name,
            'decision_trace_compression': 'gzip_mtime_zero',
            'decision_trace_sha256': sha256_file(trace_path),
            'replay_input': replay_input,
        }
        write_json(case_dir / 'artifacts.json', artifacts)
        case_manifests.append({
            'case_id': case['case_id'],
            'kind': case['kind'],
            'expected_direction': case['expected_direction'],
            **artifacts,
        })

    manifest = {
        'schema_version': 1,
        'baseline_id': args.baseline_id,
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'immutable': True,
        'purpose': 'PRE_CHANGE_REPLAY_BASELINE_NO_OVERFIT',
        'project_root': str(PROJECT_ROOT),
        'data_root': str(Path(args.data_root)),
        'runtime_code_version': cycles_document.get('code_version'),
        'runtime_strategy_config_version': cycles_document.get('strategy_config_version'),
        'code_files': code_manifest(PROJECT_ROOT),
        'secret_files': secret_manifest(),
        'journal_source': {
            'path': str(journal_path),
            'sha256_at_freeze': sha256_file(journal_path),
        },
        'cycles_source': {
            'path': str(cycles_path),
            'sha256_at_freeze': sha256_file(cycles_path),
        },
        'case_artifacts': case_manifests,
        'executed_cycle_catalog_sha256': sha256_file(
            staging / 'executed_cycle_catalog.json'
        ),
        'safety': {
            'exchange_api_called': False,
            'trading_modules_imported': False,
            'services_restarted': False,
            'secret_values_written': False,
        },
    }
    write_json(staging / 'manifest.json', manifest)
    os.chmod(staging / 'manifest.json', 0o444)
    staging.rename(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description='Freeze SMC2026 replay baseline')
    parser.add_argument('--baseline-id', required=True)
    parser.add_argument('--data-root', default=str(DEFAULT_DATA_ROOT))
    parser.add_argument('--output-root', default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument('--warmup-seconds', type=int, default=180)
    args = parser.parse_args(argv)
    output = freeze(args)
    print(json.dumps({'baseline': str(output), 'status': 'FROZEN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
