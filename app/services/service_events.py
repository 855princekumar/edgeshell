"""Record EdgeShell service starts and stops in the database.

start  -> called by run.py --serve once, before Gunicorn boots (single master)
stop   -> called by systemd ExecStopPost with SERVICE_RESULT / EXIT_* env

Each event is written to `service_events` and mirrored into `activity_logs`
(user 'system'), so it shows on the Logs page and in phpMyAdmin.

Classification of a start:
  first_start      no earlier event
  host_boot        kernel boot_id changed (EC2 reboot, stop/start, crash)
  deploy           run.py install/update restarted the service
  watchdog         watchdog restarted it after failed health checks
  crash_restart    systemd auto-restarted it after an abnormal exit
  service_restart  clean manual restart (systemctl restart)
"""
import json
import os
import socket
import subprocess
from datetime import datetime

from app import db
from app.models import ActivityLog, ServiceEvent

MARKER = os.environ.get('EDGESHELL_RESTART_MARKER',
                        '/var/lib/edgeshell/restart-reason.json')

LABELS = {
    'first_start': 'first start',
    'host_boot': 'host boot',
    'deploy': 'deploy / update',
    'watchdog': 'watchdog restart',
    'crash_restart': 'auto-restart after crash',
    'service_restart': 'manual restart',
    'host_shutdown': 'host shutdown',
    'stopped': 'stopped',
    'crash': 'crashed',
}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _boot_id():
    return _read('/proc/sys/kernel/random/boot_id') or 'unknown'


def _uptime_s():
    v = _read('/proc/uptime')
    try:
        return int(float(v.split()[0])) if v else None
    except Exception:
        return None


def _version():
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return _read(os.path.join(here, 'VERSION')) or 'unknown'


def _pop_marker():
    raw = _read(MARKER)
    if raw is None:
        return None
    try:
        os.remove(MARKER)
    except Exception:
        pass
    try:
        return json.loads(raw)
    except Exception:
        return {'reason': 'service_restart', 'detail': raw[:500]}


def _system_stopping():
    try:
        r = subprocess.run(['systemctl', 'is-system-running'], capture_output=True,
                           text=True, timeout=3)
        return r.stdout.strip() == 'stopping'
    except Exception:
        return False


def _save(ev, status):
    db.session.add(ev)
    db.session.add(ActivityLog(
        timestamp=ev.timestamp, user='system', node=ev.hostname,
        action=f"Service {ev.event}: {LABELS.get(ev.reason, ev.reason)}",
        response=ev.detail, status=status, ip_address='127.0.0.1'))
    db.session.commit()


def record_start():
    boot = _boot_id()
    last = ServiceEvent.query.order_by(ServiceEvent.id.desc()).first()
    marker = _pop_marker()
    uptime = _uptime_s()
    status = 'success'

    if last is None:
        reason, detail = 'first_start', 'First recorded start of EdgeShell on this database.'
    elif last.boot_id != boot:
        reason = 'host_boot'
        detail = f'Host booted {uptime or 0}s before service start.'
        if last.event != 'stop':
            status = 'warning'
            detail += (' Previous boot ended without a clean service stop '
                       '(hard power-off, kernel crash or forced instance stop).')
    elif marker:
        reason = marker.get('reason', 'service_restart')
        detail = marker.get('detail', '')
        status = 'warning' if reason == 'watchdog' else 'success'
    elif last.event == 'stop' and last.result not in (None, '', 'success'):
        reason, status = 'crash_restart', 'warning'
        detail = (f'systemd restarted the service after result={last.result} '
                  f'code={last.exit_code} status={last.exit_status}.')
    elif last.event == 'stop':
        reason, detail = 'service_restart', 'Clean restart.'
    else:
        reason, status = 'crash_restart', 'warning'
        detail = 'Previous process ended without running the stop hook.'

    ev = ServiceEvent(timestamp=datetime.utcnow(), event='start', reason=reason,
                      detail=detail[:512], boot_id=boot,
                      hostname=socket.gethostname()[:128], pid=os.getpid(),
                      version=_version(), host_uptime_s=uptime)
    _save(ev, status)
    return ev


def record_stop():
    result = os.environ.get('SERVICE_RESULT', '')
    code = os.environ.get('EXIT_CODE', '')
    st = os.environ.get('EXIT_STATUS', '')
    if _system_stopping():
        reason, status = 'host_shutdown', 'success'
        detail = 'Host is shutting down or rebooting.'
    elif result in ('', 'success'):
        reason, status = 'stopped', 'success'
        detail = f'Service stopped cleanly (code={code or "n/a"}).'
    else:
        reason, status = 'crash', 'failure'
        detail = f'Abnormal stop: result={result} code={code} status={st}.'
    ev = ServiceEvent(timestamp=datetime.utcnow(), event='stop', reason=reason,
                      detail=detail[:512], result=result[:32], exit_code=code[:32],
                      exit_status=st[:32], boot_id=_boot_id(),
                      hostname=socket.gethostname()[:128], pid=os.getpid(),
                      version=_version(), host_uptime_s=_uptime_s())
    _save(ev, status)
    return ev
