"""Merge current/backups without counting cumulative copies as new samples."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path


RELEVANT_EVENTS = {
    'ML_META_ACTION_STATE', 'CONTINUOUS_SCORE_SHADOW', 'RADAR_WATCH',
    'RADAR_ARMED_WINDOW', 'SETUP_OUTCOME', 'SETUP_FOLLOWUP',
    'ENTRY_FILLED', 'CYCLE_CLOSED', 'CYCLE_ABORTED', 'FEE_GATE_BLOCK',
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _sha(value):
    if not isinstance(value, (bytes, bytearray)):
        value = str(value).encode()
    return hashlib.sha256(value).hexdigest()


def _roots(base):
    parent, name = base.parent, base.name
    backup_pattern = re.compile(rf'^{re.escape(name)}\(\d+\)-$')
    rows = [
        path for path in parent.iterdir()
        if path.is_dir() and (path == base or backup_pattern.match(path.name))
    ]
    return sorted(rows, key=lambda path: (path != base, path.name))


def _event_files(root):
    yield from sorted((root / 'derived' / 'ml_meta' / 'live').glob('*.jsonl'))
    journal = root / '3_thuc_thi' / 'quan_ly_vi_the' / 'nhat_ky'
    yield from sorted(journal.glob('events*.jsonl'))


def _identity(event):
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else event
    run_id = event.get('run_id') or payload.get('run_id')
    opportunity = (
        payload.get('opportunity_id') or payload.get('semantic_key')
        or payload.get('setup_semantic_key') or payload.get('setup_id')
        or event.get('position_cycle_id')
    )
    timestamp = round(float(
        payload.get('decision_time') or event.get('ts') or 0.0
    ), 3)
    event_type = payload.get('event_type') or event.get('event') or 'UNKNOWN'
    code = payload.get('code_version') or event.get('code_version')
    scorer = payload.get('scorer_version') or payload.get('version')
    payload_hash = payload.get('payload_hash') or _sha(_canonical(payload))
    verified = bool(run_id and opportunity)
    if not opportunity:
        opportunity = f'UNRESOLVED:{payload_hash[:20]}:{timestamp:.3f}'
    dedupe = _sha(_canonical((
        run_id or 'LEGACY', opportunity, event_type, timestamp,
        payload_hash, code, scorer,
    )))
    return {
        'signature': dedupe, 'run_id': run_id, 'opportunity_id': opportunity,
        'event_type': event_type, 'decision_time': timestamp,
        'payload_hash': payload_hash, 'code_version': code,
        'scorer_version': scorer, 'identity_verified': verified,
        'payload': payload,
    }


def _insert_event(db, event, source):
    row = _identity(event)
    payload = row['payload']
    causal = isinstance(payload.get('causal_features'), dict)
    eligible = bool(
        row['identity_verified'] and causal
        and (payload.get('data_quality') or {}).get('train_eligible', False)
    )
    cursor = db.execute(
        'INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,0)',
        (
            row['signature'], row['run_id'], row['opportunity_id'],
            row['event_type'], row['decision_time'], row['payload_hash'],
            row['code_version'], row['scorer_version'], int(eligible),
            source, _canonical(payload),
        ),
    )
    if cursor.rowcount == 1:
        return True
    db.execute('UPDATE events SET duplicate_count=duplicate_count+1 WHERE signature=?', (
        row['signature'],
    ))
    return False


def _cycle_events(root):
    path = root / '3_thuc_thi' / 'quan_ly_vi_the' / 'nhat_ky' / 'cycles.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    for cycle in data.get('cycles', ()): 
        if not isinstance(cycle, dict):
            continue
        payload = {
            'event_type': 'CYCLE_SNAPSHOT',
            'opportunity_id': cycle.get('opportunity_id') or cycle.get('setup_semantic_key'),
            'setup_id': cycle.get('setup_id'), 'code_version': cycle.get('code_version'),
            'scorer_version': cycle.get('scorer_version') or cycle.get('score_version'),
            'decision_time': cycle.get('created_at'),
            'terminal_label': cycle.get('status'),
            'strategy_mainnet': cycle.get('strategy_mainnet'),
            'actual': cycle.get('actual'),
        }
        yield {'ts': cycle.get('created_at'), 'run_id': cycle.get('run_id'), 'payload': payload}


def _prune_outputs(root, keep_seconds=180 * 86400, max_bytes=2 * 1024**3):
    datasets = sorted(
        [path for path in root.iterdir() if path.is_dir() and path.name.startswith('dataset_')],
        key=lambda path: path.stat().st_mtime,
    ) if root.exists() else []
    cutoff = time.time() - keep_seconds
    for path in list(datasets):
        if path.stat().st_mtime < cutoff:
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
            datasets.remove(path)
    total = sum(child.stat().st_size for path in datasets for child in path.iterdir())
    for path in datasets:
        if total <= max_bytes:
            break
        size = sum(child.stat().st_size for child in path.iterdir())
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
        total -= size


def merge(base, output_root):
    output_root.mkdir(parents=True, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix='ml_meta_', suffix='.sqlite')
    os.close(fd)
    db = sqlite3.connect(staging)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('''CREATE TABLE events (
        signature TEXT PRIMARY KEY, run_id TEXT, opportunity_id TEXT,
        event_type TEXT, decision_time REAL, payload_hash TEXT,
        code_version TEXT, scorer_version TEXT, train_eligible INTEGER,
        source TEXT, payload TEXT, duplicate_count INTEGER
    )''')
    stats = {'files': 0, 'lines': 0, 'malformed': 0, 'duplicates': 0}
    try:
        for root in _roots(base):
            for path in _event_files(root):
                stats['files'] += 1
                try:
                    with open(path, 'r', encoding='utf-8') as handle:
                        for line in handle:
                            stats['lines'] += 1
                            try:
                                event = json.loads(line)
                            except ValueError:
                                stats['malformed'] += 1
                                continue
                            event_name = event.get('event') or (event.get('payload') or {}).get('event_type')
                            if event_name not in RELEVANT_EVENTS and 'ml_meta/live' not in str(path):
                                continue
                            inserted = _insert_event(db, event, str(path))
                            stats['duplicates'] += int(not inserted)
                except OSError:
                    continue
            for event in _cycle_events(root) or ():
                _insert_event(db, event, str(root / 'cycles.json'))
            db.commit()

        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        target = output_root / f'dataset_{stamp}'
        target.mkdir(parents=True, exist_ok=False)
        output = target / 'opportunities.jsonl'
        unresolved = target / 'unresolved_forensic.jsonl'
        opportunity_count = eligible_count = unresolved_count = 0
        with open(output, 'w', encoding='utf-8') as good, open(
            unresolved, 'w', encoding='utf-8'
        ) as bad:
            cursor = db.execute('''SELECT run_id, opportunity_id,
                MIN(decision_time), MAX(decision_time), MAX(train_eligible),
                COUNT(*), SUM(duplicate_count) FROM events
                GROUP BY run_id, opportunity_id ORDER BY MIN(decision_time)''')
            for run_id, opportunity, first, last, eligible, count, dupes in cursor:
                rows = db.execute('''SELECT event_type, decision_time, code_version,
                    scorer_version, payload, source FROM events
                    WHERE run_id IS ? AND opportunity_id=? ORDER BY decision_time''',
                    (run_id, opportunity)).fetchall()
                record = {
                    'opportunity_id': opportunity, 'run_id': run_id,
                    'first_decision_time': first, 'last_decision_time': last,
                    'event_count': count, 'deduped_copy_count': int(dupes or 0),
                    'train_eligible': bool(eligible),
                    'forensic_status': 'VERIFIED' if run_id else 'UNRESOLVED_FORENSIC',
                    'decision_chain': [
                        {'event_type': item[0], 'decision_time': item[1],
                         'code_version': item[2], 'scorer_version': item[3],
                         'payload': json.loads(item[4]), 'source': item[5]}
                        for item in rows
                    ],
                }
                opportunity_count += 1
                eligible_count += int(bool(eligible))
                if run_id:
                    good.write(_canonical(record) + '\n')
                else:
                    unresolved_count += 1
                    bad.write(_canonical(record) + '\n')
        hashes = {path.name: _sha(path.read_bytes()) for path in (output, unresolved)}
        manifest = {
            'schema_version': 'ML_META_DATASET_V1', 'created_at': time.time(),
            'source_roots': [str(item) for item in _roots(base)],
            'opportunity_count': opportunity_count,
            'train_eligible_count': eligible_count,
            'unresolved_forensic_count': unresolved_count,
            'stats': stats, 'files': hashes,
        }
        manifest['manifest_sha256'] = _sha(_canonical(manifest))
        (target / 'manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
        _prune_outputs(output_root)
        return target, manifest
    finally:
        db.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(staging + suffix)
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='/home/ubuntu/SMC2026')
    parser.add_argument('--output', default='/home/ubuntu/SMC2026/derived/ml_meta')
    args = parser.parse_args()
    target, manifest = merge(Path(args.base), Path(args.output))
    print(json.dumps({'dataset': str(target), **manifest}, ensure_ascii=False))


if __name__ == '__main__':
    main()
