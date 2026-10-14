"""Fleet metrics collector: fast, parallel, in the background.

Measured on a 122-node Tailscale fleet, the old per-request poll spent its
time on the network, not in Python: a new SSH handshake and password login
for every poll (median 0.9 s), a Python script started on the node (0.9 s,
including a 0.4 s sleep and ten subprocesses), nodes polled one after
another, and 10 s timeouts for unreachable ones. About 11 minutes for one
fleet refresh. What this module does instead:

  - Keeps one SSH connection per node open and reuses it (keepalive), so a
    poll is a single command round trip once connected.
  - Collects everything with one short shell command reading /proc
    (about 0.3 s on a Pi, including a 0.2 s CPU sample).
  - Polls the whole fleet in parallel threads (COLLECTOR_WORKERS, default 48)
    on a timer (COLLECTOR_INTERVAL, default 30 s), outside any web request.
    Pages read the stored results, so they load instantly.
  - Classifies failures: auth (wrong username or password), unreachable,
    error. Failing nodes back off instead of being retried every cycle; a
    wrong password is retried only every 10 minutes so the node's own
    fail2ban does not lock EdgeShell out. Saving new credentials retries now.

Results are written by the collector thread only (workers do SSH, never the
database), so it is safe on SQLite and MariaDB alike.
"""
import hashlib
import re
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import paramiko

INTERVAL_S = 30
WORKERS = 48
CONNECT_TIMEOUT_S = 8
COMMAND_TIMEOUT_S = 15
AUTH_RETRY_S = 600
BACKOFF_MAX_S = 300
HISTORY_EVERY_S = 60

# Unreachable nodes make paramiko log a full traceback on every attempt; the
# collector already records the reason on the node, so keep the log readable.
logging.getLogger('paramiko').setLevel(logging.CRITICAL)

# One round trip, no interpreter on the node. Sections are split by '@@'.
METRICS_CMD = (
    "head -1 /proc/stat; sleep 0.2; head -1 /proc/stat; echo @@; "
    "cat /proc/loadavg; echo @@; "
    "grep -E '^(MemTotal|MemAvailable|MemFree):' /proc/meminfo; echo @@; "
    "df -Pk / | tail -1; echo @@; "
    "cat /proc/uptime; echo @@; "
    "(command -v vcgencmd >/dev/null 2>&1 && vcgencmd measure_temp 2>/dev/null) "
    "|| cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || cat /tmp/fake_temp 2>/dev/null; echo @@; "
    "awk -F'[: ]+' 'NR>2 && $2!=\"lo\" {print $3, $11; exit}' /proc/net/dev; echo @@; "
    "uname -m; echo @@; "
    "(command -v docker >/dev/null 2>&1 && echo docker_installed); "
    "(pgrep -x dockerd >/dev/null 2>&1 && echo docker_running); "
    "(command -v node-red >/dev/null 2>&1 || ls /lib/systemd/system/nodered.service >/dev/null 2>&1) && echo nodered_installed; "
    # Match the process name (Node-RED sets it to 'node-red'), never 'pgrep -f':
    # that searches command lines, and this shell's own line mentions node-red.
    "((systemctl is-active --quiet nodered 2>/dev/null || pgrep -x node-red >/dev/null 2>&1) && echo nodered_running); "
    # OpenRemoteGUI: installed and its web gateway listening (see orgui_service).
    "( ( [ -d /opt/openremotegui ] || [ -d /etc/openremotegui ] || [ -d \"$HOME/OpenRemoteGUI\" ] ) "
    "&& echo orgui_installed && (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) "
    "| grep -qE '[:.]__ORGUI_PORT__([^0-9]|$)' && echo orgui_running ); echo @@"
)


# ---------------------------------------------------------------- parsing

def _cpu_percent(line1, line2):
    a = [int(x) for x in line1.split()[1:]]
    b = [int(x) for x in line2.split()[1:]]
    total = sum(b) - sum(a)
    idle = (b[3] + (b[4] if len(b) > 4 else 0)) - (a[3] + (a[4] if len(a) > 4 else 0))
    return round((1 - idle / total) * 100, 1) if total > 0 else 0.0


def _uptime_str(seconds):
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    return (f'{d}d ' if d else '') + f'{h}h {r // 60}m'


def parse_metrics(output):
    """Parse METRICS_CMD output into the metrics dict the UI expects."""
    parts = [p.strip() for p in output.split('@@')]
    stat = [l for l in parts[0].splitlines() if l.startswith('cpu ')]
    cpu = _cpu_percent(stat[0], stat[1]) if len(stat) >= 2 else 0.0
    la = (parts[1].split() + ['0', '0', '0'])[:3]
    load = [float(x) for x in la]
    mem = {}
    for line in parts[2].splitlines():
        k, _, v = line.partition(':')
        try:
            mem[k.strip()] = int(v.split()[0])
        except (ValueError, IndexError):
            pass
    total = mem.get('MemTotal', 0) or 1
    avail = mem.get('MemAvailable', mem.get('MemFree', 0))
    used = total - avail
    df = parts[3].split()
    try:
        disk_total_k, disk_used_k = int(df[1]), int(df[2])
        disk_pct = round(disk_used_k / (int(df[2]) + int(df[3])) * 100, 1)
    except (ValueError, IndexError, ZeroDivisionError):
        disk_total_k = disk_used_k = 0
        disk_pct = 0.0
    try:
        uptime_s = float(parts[4].split()[0])
    except (ValueError, IndexError):
        uptime_s = 0.0
    temp = None
    t = parts[5].strip()
    if 'temp=' in t:
        try:
            temp = float(t.split('=')[1].split("'")[0])
        except ValueError:
            pass
    elif t.lstrip('-').isdigit():
        temp = round(int(t) / 1000, 1)
    rx = tx = 0
    net = parts[6].split()
    if len(net) >= 2 and net[0].isdigit() and net[1].isdigit():
        rx, tx = int(net[0]), int(net[1])
    flags = set(parts[8].split()) if len(parts) > 8 else set()
    return {
        'cpu_percent': cpu,
        'mem_percent': round(used / total * 100, 1),
        'mem_total_mb': round(total / 1024),
        'mem_used_mb': round(used / 1024),
        'mem_free_mb': round(avail / 1024),
        'disk_percent': disk_pct,
        'disk_total_gb': round(disk_total_k / 1048576, 1),
        'disk_used_gb': round(disk_used_k / 1048576, 1),
        'load_1': load[0], 'load_5': load[1], 'load_15': load[2],
        'load_trend': 'rising' if load[0] > load[2] else ('falling' if load[0] < load[2] else 'stable'),
        'uptime': _uptime_str(uptime_s),
        'uptime_seconds': int(uptime_s),
        'temperature': temp,
        'arch': parts[7].strip() if len(parts) > 7 else '',
        'net_rx_bytes': rx,
        'net_tx_bytes': tx,
        'docker_installed': 'docker_installed' in flags,
        'docker_running': 'docker_running' in flags,
        'nodered_installed': 'nodered_installed' in flags,
        'nodered_running': 'nodered_running' in flags,
        'orgui_installed': 'orgui_installed' in flags,
        'orgui_running': 'orgui_running' in flags,
        'collected_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


# ---------------------------------------------------------------- connections

class _Pool:
    """One open SSH connection per node, keyed by its connection details so
    edited credentials always get a fresh login."""

    def __init__(self):
        self._conns = {}
        self._locks = {}
        self._guard = threading.Lock()

    @staticmethod
    def key(t):
        pw = hashlib.sha256((t['password'] or '').encode()).hexdigest()[:16]
        return (t['ip'], t['port'], t['username'], pw)

    def lock(self, node_id):
        with self._guard:
            return self._locks.setdefault(node_id, threading.Lock())

    def get(self, t):
        entry = self._conns.get(t['id'])
        if entry and entry[0] == self.key(t):
            transport = entry[1].get_transport()
            if transport and transport.is_active():
                return entry[1], False
            self.drop(t['id'])
        elif entry:
            self.drop(t['id'])
        from app.services.ssh_service import connect
        client = connect(t['ip'], t['port'], t['username'], t['password'], CONNECT_TIMEOUT_S)
        client.get_transport().set_keepalive(20)
        self._conns[t['id']] = (self.key(t), client)
        return client, True

    def drop(self, node_id):
        entry = self._conns.pop(node_id, None)
        if entry:
            try:
                entry[1].close()
            except Exception:
                pass

    def size(self):
        return len(self._conns)


POOL = _Pool()


def classify(exc):
    """('auth' | 'unreachable' | 'error', human message) for an exception."""
    from app.services.ssh_service import (TailscaleCheckRequired, TAILSCALE_CHECK_MESSAGE,
                                          LoginPaused, PAUSED_MESSAGE)
    if isinstance(exc, TailscaleCheckRequired):
        return 'auth', TAILSCALE_CHECK_MESSAGE
    if isinstance(exc, LoginPaused):
        return 'auth', PAUSED_MESSAGE
    if isinstance(exc, paramiko.AuthenticationException):
        return 'auth', 'Authentication failed: check the SSH username and password'
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return 'unreachable', 'No answer from the node (timed out)'
    if isinstance(exc, (ConnectionRefusedError,)):
        return 'unreachable', 'Connection refused on the SSH port'
    if isinstance(exc, (socket.gaierror,)):
        return 'unreachable', 'Unknown host name'
    if isinstance(exc, OSError) and not isinstance(exc, paramiko.SSHException):
        return 'unreachable', f'Network error: {exc.strerror or exc}'
    if isinstance(exc, paramiko.SSHException):
        return 'error', f'SSH error: {exc}'
    return 'error', f'{type(exc).__name__}: {exc}'


def collect_one(target):
    """Collect one node. target: plain dict (id, ip, port, username, password).
    Returns {'ok', 'metrics', 'kind', 'message', 'seconds', 'reconnected'}."""
    started = time.time()
    with POOL.lock(target['id']):
        for attempt in (1, 2):
            try:
                client, fresh = POOL.get(target)
                cmd = METRICS_CMD.replace('__ORGUI_PORT__', str(int(target.get('orgui_port') or 6080)))
                _, out, _ = client.exec_command(cmd, timeout=COMMAND_TIMEOUT_S)
                text = out.read().decode('utf-8', 'replace')
                metrics = parse_metrics(text)
                return {'ok': True, 'metrics': metrics, 'kind': None, 'message': None,
                        'seconds': round(time.time() - started, 2), 'reconnected': fresh}
            except Exception as e:
                POOL.drop(target['id'])
                kind, message = classify(e)
                # A pooled connection the node closed: retry once with a new login.
                if attempt == 1 and kind == 'error' and not isinstance(e, paramiko.AuthenticationException):
                    continue
                from app.services.ssh_service import TailscaleCheckRequired
                return {'ok': False, 'metrics': None, 'kind': kind, 'message': message,
                        'reason': 'tailscale' if isinstance(e, TailscaleCheckRequired) else 'password',
                        'banner': getattr(e, 'ssh_banner', None),
                        'seconds': round(time.time() - started, 2), 'reconnected': True}


# ---------------------------------------------------------------- scheduling

class Collector:
    def __init__(self):
        self.latest = {}            # node_id -> last result (metrics or error)
        self.next_at = {}           # node_id -> earliest next attempt (epoch)
        self.failures = {}          # node_id -> consecutive failures
        self.last_sample = 0        # epoch of the last history sample of the fleet
        self.auth_fails = {}        # node_id -> password failures in a row (login policy)
        self.canary_at = {}         # node_id -> epoch of its last Tailscale canary login
        self.last_login_recheck = 0  # first check soon after start (banners only)
        self._last_rollup = 0
        self.cycle = {'started': None, 'seconds': None, 'nodes': 0, 'ok': 0,
                      'auth': 0, 'unreachable': 0, 'error': 0, 'skipped': 0}
        self._wake = threading.Event()
        self._thread = None
        self._app = None
        self._last_purge = 0

    # -- control
    def start(self, app):
        if self._thread and self._thread.is_alive():
            return
        self._app = app
        self._thread = threading.Thread(target=self._loop, name='metrics-collector', daemon=True)
        self._thread.start()

    def kick(self, node_ids=None):
        """Collect these nodes (or every node) at the next opportunity."""
        for nid in (node_ids or list(self.next_at)):
            self.next_at[nid] = 0
        if not node_ids:
            self.next_at.clear()
        self._wake.set()

    def _loop(self):
        while True:
            try:
                with self._app.app_context():
                    self.run_cycle()
                    self._roll_up()
                    self._purge_history()
                    from app.services import login_policy
                    login_policy.recheck(self)
            except Exception as e:
                self._app.logger.warning('metrics cycle failed: %s', e)
            interval = int(self._app.config.get('COLLECTOR_INTERVAL', INTERVAL_S))
            self._wake.wait(timeout=max(5, interval))
            self._wake.clear()

    # -- one pass over the fleet
    def run_cycle(self, node_ids=None, force=False, manual=False):
        """Collect due nodes in parallel and store the results. Returns
        {node_id: result}. Must run inside an app context.

        Nodes whose logins are paused are left out, also when forced, unless
        manual: one operator action on one node (Test, Fix login) may try
        once; bulk actions never do."""
        from flask import current_app
        from app import db
        from app.models.node import Node
        now = time.time()
        q = Node.query.filter(Node.is_active.is_(True), Node.is_deleted.isnot(True))
        if node_ids:
            q = q.filter(Node.id.in_(node_ids))
        nodes = q.all()
        due = [n for n in nodes if (force or self.next_at.get(n.id, 0) <= now)
               and (manual or n.login_paused_at is None)]
        targets = [{'id': n.id, 'ip': n.ip_address, 'port': n.ssh_port or 22,
                    'username': n.username, 'password': n.password,
                    'orgui_port': n.orgui_port or 6080} for n in due]
        workers = int(current_app.config.get('COLLECTOR_WORKERS', WORKERS))
        started = time.time()
        results = {}
        if targets:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets)))) as pool:
                for t, r in zip(targets, pool.map(collect_one, targets)):
                    results[t['id']] = r
        self._store(nodes, results, now)
        from app.services import login_policy
        login_policy.apply(nodes, results, self)
        if not node_ids and now - self.last_sample >= HISTORY_EVERY_S:
            # one history sample per node per minute, whatever its state
            from app.services import history
            self.last_sample = now
            db.session.add_all(history.sample_rows(nodes, self.latest, fresh_s=HISTORY_EVERY_S * 2))
        db.session.commit()
        if not node_ids:
            counts = {'ok': 0, 'auth': 0, 'unreachable': 0, 'error': 0}
            for r in results.values():
                counts['ok' if r['ok'] else r['kind']] += 1
            self.cycle = dict(counts, started=started, seconds=round(time.time() - started, 2),
                              nodes=len(nodes), skipped=len(nodes) - len(due))
        return results

    def _store(self, nodes, results, now):
        by_id = {n.id: n for n in nodes}
        for nid, r in results.items():
            n = by_id[nid]
            self.latest[nid] = dict(r, at=datetime.utcnow())
            if r['ok']:
                m = r['metrics']
                self.failures[nid] = 0
                self.next_at[nid] = 0
                n.cpu_percent, n.ram_percent = m['cpu_percent'], m['mem_percent']
                n.disk_percent, n.temperature = m['disk_percent'], m['temperature']
                n.load_average = f"{m['load_1']} {m['load_5']} {m['load_15']}"
                n.uptime = m['uptime']
                if m['arch']:
                    n.arch = m['arch']
                n.docker_installed = m['docker_installed']
                n.nodered_installed = m['nodered_installed']
                n.orgui_installed = m['orgui_running']      # usable only when running
                n.orgui_checked_at = datetime.utcnow()
                n.status, n.last_error = 'online', None
                n.last_seen = datetime.utcnow()
            else:
                fails = self.failures.get(nid, 0) + 1
                self.failures[nid] = fails
                if r['kind'] == 'auth':
                    n.status = 'auth_failed'
                    self.next_at[nid] = now + AUTH_RETRY_S
                else:
                    n.status = 'offline'
                    self.next_at[nid] = now + min(BACKOFF_MAX_S, INTERVAL_S * 2 ** (fails - 1))
                n.last_error = (r['message'] or '')[:250]

    def _roll_up(self):
        """Hourly rollups for the long-term history (a few seconds an hour)."""
        if time.time() - self._last_rollup < 300:
            return
        self._last_rollup = time.time()
        from app.services import history
        history.roll_up(max_hours=24)          # a first backfill spreads over a few passes

    def _purge_history(self):
        from flask import current_app
        from app import db
        from app.models import SystemMetrics
        if time.time() - self._last_purge < 6 * 3600:
            return
        self._last_purge = time.time()
        days = int(current_app.config.get('METRICS_RETENTION_DAYS', 14))
        cutoff = datetime.utcnow() - timedelta(days=days)
        SystemMetrics.query.filter(SystemMetrics.timestamp < cutoff).delete(synchronize_session=False)
        db.session.commit()

    def status(self):
        return dict(self.cycle, pooled_connections=POOL.size(),
                    running=bool(self._thread and self._thread.is_alive()))


COLLECTOR = Collector()


def start(app):
    if app.config.get('COLLECTOR_ENABLED', True) and not app.config.get('TESTING'):
        COLLECTOR.start(app)


# ---------------------------------------------------------------- commands

# sudo inside this one command reads the password from a shell variable that
# came in on stdin: never on a command line, never echoed, no reliance on
# sudo's credential cache (which does not carry across pipes).
_SUDO_PRIME = ("IFS= read -r __es_pw; "
               "sudo() { printf '%s' \"$__es_pw\" | command sudo -S -p '' \"$@\"; }; ")


def run_command(target, command, timeout=60, sudo_password=None, on_output=None,
                get_pty=True, stop=None):
    """Run a command on the node over its pooled connection (no new login).

    sudo: when the command uses sudo and a password is known, every sudo in it
    gets the password on stdin (sent over the SSH channel, never on a command
    line, so it is not visible in the node's process list) and the command
    runs without a terminal, so nothing is echoed.
    on_output(text) receives output as it arrives; stop() may end it early.
    Returns {'stdout', 'exit_code', 'success', 'duration', 'error', 'kind'}."""
    started = time.time()
    uses_sudo = sudo_password and re.search(r'(^|[\s;&|(])sudo\s', command)
    if uses_sudo:
        command = _SUDO_PRIME + command
        get_pty = False
    out = []
    if target.get('login_paused'):
        from app.services.ssh_service import PAUSED_MESSAGE
        return {'stdout': '', 'exit_code': -1, 'success': False, 'duration': 0,
                'error': PAUSED_MESSAGE, 'kind': 'auth'}
    try:
        with POOL.lock(target['id']):
            client, _ = POOL.get(target)
        chan = client.get_transport().open_session(timeout=CONNECT_TIMEOUT_S)
        if get_pty:
            chan.get_pty(term='xterm', width=200, height=50)
        chan.set_combine_stderr(True)
        chan.exec_command(command)
        if uses_sudo:
            chan.sendall((sudo_password + '\n').encode())
        deadline = started + timeout
        while True:
            if chan.recv_ready():
                text = chan.recv(65536).decode('utf-8', 'replace')
                out.append(text)
                if on_output:
                    on_output(text)
                continue
            if chan.exit_status_ready() and not chan.recv_ready():
                break
            if time.time() > deadline or (stop and stop()):
                chan.close()
                msg = 'Stopped' if stop and stop() else f'Timed out after {timeout}s'
                return {'stdout': ''.join(out), 'exit_code': -1, 'success': False,
                        'duration': round(time.time() - started, 2), 'error': msg, 'kind': 'timeout'}
            time.sleep(0.05)
        code = chan.recv_exit_status()
        chan.close()
        return {'stdout': ''.join(out), 'exit_code': code, 'success': code == 0,
                'duration': round(time.time() - started, 2), 'error': None, 'kind': None}
    except Exception as e:
        POOL.drop(target['id'])
        kind, message = classify(e)
        return {'stdout': ''.join(out), 'exit_code': -1, 'success': False,
                'duration': round(time.time() - started, 2), 'error': message, 'kind': kind}


def target_for(node):
    """Connection details for run_command / collect_one (decrypts the password)."""
    return {'id': node.id, 'ip': node.ip_address, 'port': node.ssh_port or 22,
            'username': node.username, 'password': node.password,
            'orgui_port': node.orgui_port or 6080, 'name': node.name,
            'login_paused': node.login_paused_at is not None}
