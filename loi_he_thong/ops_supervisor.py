"""Out-of-process service health monitor; never participates in trading."""

import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


DATA_ROOT = Path(os.getenv('SMC_RECORDER_DATA_ROOT', '/home/ubuntu/smc2026_data'))
HEALTH_ROOT = DATA_ROOT / 'health'
BOT_HEARTBEAT = HEALTH_ROOT / 'bot_runtime.json'
RECORDER_HEALTH = HEALTH_ROOT / 'status.json'
RECORDER_DISABLED = os.getenv('SMC_RECORDER_POLICY', 'ENABLED').upper() == 'DISABLED'
OUTPUT = HEALTH_ROOT / 'system_status.json'
INTERVAL_SECONDS = float(os.getenv('SMC_OPS_HEALTH_SECONDS', '5'))
STALE_SECONDS = float(os.getenv('SMC_OPS_STALE_SECONDS', '15'))
RESTART_COOLDOWN_SECONDS = float(os.getenv('SMC_OPS_RESTART_COOLDOWN_SECONDS', '60'))
CRITICAL_LOOP_GRACE_SECONDS = float(os.getenv('SMC_OPS_CRITICAL_LOOP_GRACE_SECONDS', '10'))
BIAS_ENTRY_STALE_SECONDS = float(os.getenv('SMC_OPS_BIAS_ENTRY_STALE_SECONDS', '5'))
GUARDIAN_STALE_SECONDS = float(os.getenv('SMC_OPS_GUARDIAN_STALE_SECONDS', '2'))

SERVICES = {
    'bot': 'smc2026-bot.service',
    'recorder': 'smc2026-recorder.service',
    'gemini_shadow': 'smc2026-gemini-shadow.service',
    'health': 'smc2026-health.service',
}


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='system_health_', suffix='.tmp', dir=path.parent
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}


def _service_state(unit):
    command = [
        'systemctl', 'show', unit,
        '--property=ActiveState,SubState,MainPID,NRestarts',
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=3, check=False,
    )
    values = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition('=')
        if separator:
            values[key] = value
    active = values.get('ActiveState', '')
    sub = values.get('SubState', '')
    pid = values.get('MainPID', '')
    restarts = values.get('NRestarts', '')
    try:
        pid_value = int(pid or 0)
    except ValueError:
        pid_value = 0
    try:
        restart_value = int(restarts or 0)
    except ValueError:
        restart_value = 0
    return {
        'unit': unit,
        'active_state': active or 'unknown',
        'sub_state': sub or 'unknown',
        'pid': pid_value,
        'restarts': restart_value,
        'query_error': completed.stderr.strip()[:300] if completed.returncode else None,
    }


def _process_resources(pid):
    if pid <= 0:
        return {'rss_bytes': 0, 'cpu_percent': 0.0}
    completed = subprocess.run(
        ['ps', '-p', str(pid), '-o', '%cpu=,rss='],
        capture_output=True, text=True, timeout=2, check=False,
    )
    fields = completed.stdout.split()
    if len(fields) != 2:
        return {'rss_bytes': 0, 'cpu_percent': 0.0}
    try:
        return {
            'cpu_percent': max(0.0, float(fields[0])),
            'rss_bytes': max(0, int(fields[1])) * 1024,
        }
    except ValueError:
        return {'rss_bytes': 0, 'cpu_percent': 0.0}


def _heartbeat_age(payload, now_ms):
    updated = int(payload.get('updated_at_ms', 0) or 0)
    return None if updated <= 0 else max(0.0, (now_ms - updated) / 1000.0)


def _restart_stalled_bot(pid):
    if pid > 0:
        try:
            os.kill(pid, signal.SIGUSR1)
            time.sleep(0.5)
            # A blocked/stopped process may never honor systemd's graceful
            # SIGTERM.  Restart=always is the owner of recovery after this bounded diagnostic dump.
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _critical_loop_classification(bot_payload, now):
    installed_at = float(bot_payload.get('critical_liveness_installed_at', 0.0) or 0.0)
    if installed_at <= 0.0 or now - installed_at < CRITICAL_LOOP_GRACE_SECONDS:
        return None

    loops = bot_payload.get('critical_loops') or {}
    limits = {
        'bias': BIAS_ENTRY_STALE_SECONDS,
        'entry': BIAS_ENTRY_STALE_SECONDS,
    }
    if bool(bot_payload.get('shadow_position_active', False)):
        limits['guardian'] = GUARDIAN_STALE_SECONDS

    for name, limit in limits.items():
        item = loops.get(name) or {}
        age = item.get('age_sec')
        consecutive = int(item.get('consecutive_errors', 0) or 0)
        if age is None or float(age) > limit or consecutive >= 3:
            return f"{name.upper()}_LOOP_STALLED"
    return None


def build_snapshot(now=None):
    now = time.time() if now is None else float(now)
    now_ms = int(now * 1000)
    services = {name: _service_state(unit) for name, unit in SERVICES.items()}
    for value in services.values():
        value.update(_process_resources(value['pid']))

    bot_payload = _read_json(BOT_HEARTBEAT)
    recorder_payload = _read_json(RECORDER_HEALTH)
    bot_age = _heartbeat_age(bot_payload, now_ms)
    recorder_age = _heartbeat_age(recorder_payload, now_ms)

    bot_active = services['bot']['active_state'] == 'active'
    recorder_active = services['recorder']['active_state'] == 'active'
    bot_stalled = bool(bot_active and (bot_age is None or bot_age > STALE_SECONDS))
    critical_stall = _critical_loop_classification(bot_payload, now) if bot_active else None
    recorder_stale = bool(
        not RECORDER_DISABLED
        and (not recorder_active or recorder_age is None or recorder_age > STALE_SECONDS)
    )

    if not bot_active:
        bot_classification = 'PROCESS_DOWN'
    elif bot_stalled:
        bot_classification = 'EVENT_LOOP_STALLED'
    elif critical_stall:
        bot_classification = critical_stall
    elif not bot_payload.get('system_ready', False):
        bot_classification = 'SAFETY_BLOCK'
    else:
        bot_classification = 'IDLE_MARKET'

    return {
        'schema_version': 1,
        'updated_at_ms': now_ms,
        'status': 'ERROR' if (not bot_active or bot_stalled or critical_stall or recorder_stale) else 'RUNNING',
        'services': services,
        'bot': {
            'classification': bot_classification,
            'heartbeat_age_seconds': bot_age,
            'heartbeat': bot_payload,
        },
        'recorder': {
            'classification': 'DISABLED_BY_POLICY' if RECORDER_DISABLED else (
                'PROCESS_DOWN' if not recorder_active else (
                    'STALE_PROCESS' if recorder_stale else 'RUNNING'
                )
            ),
            'heartbeat_age_seconds': recorder_age,
            'reported_status': recorder_payload.get('current_status'),
        },
    }


def run_forever():
    last_bot_restart = 0.0
    while True:
        started = time.time()
        try:
            snapshot = build_snapshot(started)
            bot = snapshot['bot']
            if (
                bot['classification'] in {
                    'EVENT_LOOP_STALLED',
                    'BIAS_LOOP_STALLED',
                    'ENTRY_LOOP_STALLED',
                    'GUARDIAN_LOOP_STALLED',
                }
                and started - last_bot_restart >= RESTART_COOLDOWN_SECONDS
            ):
                pid = int(snapshot['services']['bot'].get('pid', 0) or 0)
                logging.critical(
                    '[OPS] Bot critical loop stalled (%s); dump stack then restart pid=%s',
                    bot['classification'], pid
                )
                _restart_stalled_bot(pid)
                last_bot_restart = started
                snapshot['bot']['restart_requested'] = True
            _atomic_json(OUTPUT, snapshot)
        except Exception:
            logging.exception('[OPS] Health supervisor iteration failed')
        elapsed = time.time() - started
        time.sleep(max(0.2, INTERVAL_SECONDS - elapsed))


def main():
    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s'
    )
    run_forever()


if __name__ == '__main__':
    main()
