"""Supervisor passivo: detecta ausência de progresso e coleta evidências limitadas."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import time

import psutil

from jobs import JobStore, data_dir, instant, now

SNAPSHOT_LIMIT = 256 * 1024
SECRET = re.compile(
    r'(?i)(authorization\s*[:=]\s*|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)[^\s,;"}]+')


def _age(timestamp, current):
    if not timestamp:
        return None
    return max(0, (current - instant(timestamp)).total_seconds())


def _docker_state(container_name):
    try:
        result = subprocess.run(
            ['docker', 'inspect', '--format',
             '{{json .State}}', container_name], capture_output=True, text=True,
            timeout=2, check=False)
        if result.returncode:
            return None, result.stderr.strip()[:500] or 'docker inspect falhou'
        return json.loads(result.stdout), None
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)[:500]


def capture_snapshot(store, incident_id, row, seconds_without_progress):
    """Grava somente metadados sanitizados; falhas viram warnings."""
    warnings = []
    container = ('qh-' + row['id'].replace('_', '-') + '-' + row['active_token'][:12])
    docker, warning = _docker_state(container)
    if warning:
        warnings.append('Docker: ' + warning)
    try:
        process = psutil.Process(int(row.get('pid') or os.getpid())) if row.get('pid') else None
        process_data = process.as_dict(attrs=['pid', 'ppid', 'status', 'cpu_percent', 'memory_info']) if process else None
        if process_data and process_data.get('memory_info'):
            process_data['memory_info'] = process_data['memory_info']._asdict()
    except (psutil.Error, ValueError, OSError) as exc:
        process_data = None
        warnings.append('Processo: ' + str(exc)[:300])
    envelope = {
        'schema_version': 1,
        'incident_id': incident_id,
        'captured_at': now(),
        'scope': 'docker_attempt',
        'session_id': row['session_id'],
        'job_id': row['id'],
        'attempt_id': row['active_token'],
        'worker_instance_id': row.get('worker_instance_id'),
        'execution_status': row['status'],
        'phase': row.get('phase'),
        'signals': {
            'last_activity_at': row.get('last_activity_at'),
            'last_progress_at': row.get('last_progress_at'),
            'seconds_without_progress': int(seconds_without_progress),
        },
        'observations': {
            'host': {
                'cpu_percent': psutil.cpu_percent(interval=None),
                'memory': psutil.virtual_memory()._asdict(),
                'disk': psutil.disk_usage(str(store.base))._asdict(),
            },
            'process': process_data,
            'docker_state': docker,
        },
        'capture_warnings': warnings,
        'automatic_recovery_started': False,
    }
    raw = SECRET.sub(r'\1[REDACTED]', json.dumps(envelope, ensure_ascii=False, indent=2, default=str))
    encoded = raw.encode('utf-8')
    if len(encoded) > SNAPSHOT_LIMIT:
        warnings.append('Snapshot truncado no limite de 256 KiB.')
        envelope['observations'] = {'omitted': 'Limite de tamanho atingido.'}
        envelope['capture_warnings'] = warnings
        encoded = SECRET.sub(
            r'\1[REDACTED]',
            json.dumps(envelope, ensure_ascii=False, indent=2, default=str),
        ).encode('utf-8')
    relative = Path('diagnostics') / incident_id / 'snapshot.json'
    target = store.base / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        return str(relative), sha256(encoded).hexdigest(), warnings
    except OSError as exc:
        warnings.append('Persistência: ' + str(exc)[:300])
        return None, None, warnings


def evaluate(store, current=None, suspected_seconds=None, stalled_seconds=None):
    """Executa um ciclo determinístico do supervisor."""
    current = instant(current)
    suspected = (int(os.environ.get('HARNESS_STALL_SUSPECT_SECONDS', '120'))
                 if suspected_seconds is None else suspected_seconds)
    stalled = (int(os.environ.get('HARNESS_STALL_CONFIRM_SECONDS', '240'))
               if stalled_seconds is None else stalled_seconds)
    if suspected <= 0 or stalled < suspected:
        raise ValueError('Limiares do supervisor são inválidos.')
    opened = []
    store.resolve_incidents()
    for row in store.active_attempts():
        heartbeat_age = _age(row.get('last_heartbeat_at'), current)
        if heartbeat_age is not None and heartbeat_age >= suspected:
            health = 'STALLED' if heartbeat_age >= stalled else 'SUSPECTED_STALL'
            detail = f'Worker sem sinal de vida há {int(heartbeat_age)} s.'
            incident_id, created = store.open_incident(
                row, health, 'WORKER_HEARTBEAT_LOST', suspected, detail)
            if created:
                path, digest, warnings = capture_snapshot(
                    store, incident_id, row, heartbeat_age)
                store.attach_snapshot(incident_id, path, digest, warnings)
                opened.append(incident_id)
        baseline = row.get('last_progress_at') or row.get('started_at') or row.get('created_at')
        age = _age(baseline, current)
        if age is None or age < suspected:
            continue
        has_progress_signal = bool(row.get('last_progress_at'))
        health = 'STALLED' if has_progress_signal and age >= stalled else 'SUSPECTED_STALL'
        trigger = 'NO_OBSERVED_PROGRESS' if has_progress_signal else 'NO_PROGRESS_TELEMETRY'
        detail = (f'{int(age)} s desde o último progresso observado.' if has_progress_signal
                  else f'{int(age)} s sem telemetria de progresso; imobilidade não comprovada.')
        incident_id, created = store.open_incident(row, health, trigger, suspected, detail)
        if created:
            path, digest, warnings = capture_snapshot(store, incident_id, row, age)
            store.attach_snapshot(incident_id, path, digest, warnings)
            opened.append(incident_id)
    return opened


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float,
                        default=float(os.environ.get('HARNESS_SUPERVISOR_INTERVAL', '5')))
    args = parser.parse_args()
    if args.interval <= 0:
        raise SystemExit('O intervalo precisa ser positivo.')
    store = JobStore(data_dir())
    print('Supervisor passivo ativo; nenhuma recuperação automática será executada.', flush=True)
    while True:
        try:
            evaluate(store)
        except Exception as exc:
            print(f'Falha no ciclo do supervisor: {exc}', flush=True)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
