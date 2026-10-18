"""MQTT heartbeat: a second, VPN-independent liveness signal per node.

Each node runs the heartbeat agent (app/agents/heartbeat, a pinned copy of
github.com/855princekumar/mqtt-heartbeat-service). It publishes encrypted,
authenticated heartbeats and a signed last will (LWT) on a public broker;
SenseHive verifies them; EdgeShell reads SenseHive's read-only API and uses
the result for node cards and uptime (SLA), so a VPN glitch is no longer
counted as node downtime. See docs/adr/0013-mqtt-heartbeat.md.

EdgeShell holds the fleet master key (HEARTBEAT_MASTER_KEY) only to give each
node its own keys at install time; it never sends the master key to a node.
"""
import base64
import hashlib
import json
import os
import posixpath
import re
import time
from datetime import datetime

from flask import current_app

from app import db

AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agents', 'heartbeat')
AGENT_FILES = ('heartbeat_agent.py', 'heartbeat_protocol.py', 'install.sh', 'setup.py')
REMOTE_HOME = '/opt/edgeshell-heartbeat'
SETTINGS_KEY = 'heartbeat_agent_settings'
FRESH_S = 40                     # a heartbeat newer than this counts the node as up
SYNC_EVERY_S = 15


def protocol():
    import importlib.util
    spec = importlib.util.spec_from_file_location('edgeshell_heartbeat_protocol',
                                                  os.path.join(AGENT_DIR, 'heartbeat_protocol.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def agent_version():
    with open(os.path.join(AGENT_DIR, 'heartbeat_agent.py'), encoding='utf-8') as fh:
        for line in fh:
            if line.startswith('__version__'):
                return line.split('=', 1)[1].strip().strip('\'"')
    return 'unknown'


def master_key():
    raw = (current_app.config.get('HEARTBEAT_MASTER_KEY') or '').strip()
    if not raw:
        return None
    key = bytes.fromhex(raw)
    if len(key) != 32:
        raise ValueError('HEARTBEAT_MASTER_KEY must be 64 hex characters')
    return key


def enabled():
    try:
        return master_key() is not None
    except ValueError:
        return False


def sensehive_configured():
    cfg = current_app.config
    return bool(cfg.get('SENSEHIVE_URL') and cfg.get('SENSEHIVE_API_TOKEN'))


# ------------------------------------------------------------------ fleet settings
DEFAULTS = {'broker': 'broker.hivemq.com', 'port': 8883, 'tls': True, 'interval': 10}


def agent_settings():
    """The fleet-wide config.yaml values: saved by an administrator, else the
    server defaults (HEARTBEAT_BROKER, _PORT, _TLS, _INTERVAL)."""
    from app.models import Settings
    cfg = current_app.config
    out = {'broker': cfg.get('HEARTBEAT_BROKER') or DEFAULTS['broker'],
           'port': int(cfg.get('HEARTBEAT_PORT') or DEFAULTS['port']),
           'tls': str(cfg.get('HEARTBEAT_TLS', 'true')).lower() in ('1', 'true', 'yes', 'on'),
           'interval': int(cfg.get('HEARTBEAT_INTERVAL') or DEFAULTS['interval'])}
    row = Settings.query.filter_by(key=SETTINGS_KEY).first()
    if row and row.value:
        try:
            saved = json.loads(row.value)
            out.update({k: saved[k] for k in DEFAULTS if k in saved})
        except ValueError:
            pass
    return out


def validate_settings(data):
    """Same rules as the agent, so a pushed file is never rejected on a node."""
    import re
    s = dict(agent_settings())
    s.update({k: data[k] for k in DEFAULTS if k in data})
    if not isinstance(s['broker'], str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', s['broker']):
        return None, 'Broker must be a host name'
    try:
        s['port'], s['interval'] = int(s['port']), int(s['interval'])
    except (TypeError, ValueError):
        return None, 'Port and interval must be numbers'
    if not 1 <= s['port'] <= 65535:
        return None, 'Port must be 1 to 65535'
    if not 5 <= s['interval'] <= 3600:
        return None, 'Interval must be 5 to 3600 seconds'
    s['tls'] = s['tls'] in (True, 'true', 'on', '1', 1)
    return s, None


def save_settings(s):
    from app.models import Settings
    row = Settings.query.filter_by(key=SETTINGS_KEY).first()
    value = json.dumps(s, sort_keys=True)
    if row:
        row.value = value
    else:
        db.session.add(Settings(key=SETTINGS_KEY, value=value))
    db.session.commit()


def config_yaml(s=None):
    s = s or agent_settings()
    return ('# Pushed by EdgeShell (Fleet Setup, Heartbeat agent). Hot reloaded by the agent;\n'
            '# no secrets here. An invalid file is ignored and the last good settings stay.\n'
            f"broker: {s['broker']}\nport: {int(s['port'])}\ntls: {'true' if s['tls'] else 'false'}\n"
            f"interval: {int(s['interval'])}\n")


def agent_env(node):
    """This node's identity and keys (never the master key)."""
    p = protocol().provision(master_key(), node.name)
    return ''.join(f'{k}={v}\n' for k, v in p.items())


def join_token(node, s=None):
    """The node's identity and the fleet broker as a SenseHive join token (the agent's
    setup.py reads it; same format as SenseHive's Add node). Never the master key."""
    s = s or agent_settings()
    p = protocol().provision(master_key(), node.name)
    body = {'v': 1, 'node': node.name, 'kid': p['HB_KID'], 'nk': p['HB_NODE_KEY'], 'tk': p['HB_TOPIC_KEY'],
            'broker': s['broker'], 'port': int(s['port']), 'tls': bool(s['tls'])}
    body['c'] = hashlib.sha256(json.dumps(body, separators=(',', ':'), sort_keys=True).encode()).hexdigest()[:8]
    raw = json.dumps(body, separators=(',', ':'), sort_keys=True).encode()
    return 'hbj1.' + base64.urlsafe_b64encode(raw).decode().rstrip('=')


def broker_login():
    """The broker login every node uses (HEARTBEAT_BROKER_USERNAME / _PASSWORD)."""
    cfg = current_app.config
    return (cfg.get('HEARTBEAT_BROKER_USERNAME') or 'admin'), (cfg.get('HEARTBEAT_BROKER_PASSWORD') or 'admin123')


def login_env():
    user, password = broker_login()
    return f'HB_MQTT_USERNAME={user}\nHB_MQTT_PASSWORD={password}\n'


WAY = re.compile(r'^Broker: \S+ over (\S+) on port (\d+)', re.M)


# ------------------------------------------------------------------ on the node
def _upload(target, files):
    """Copy {name: text} into a fresh private directory on the node; return it."""
    from app.services.collector import POOL
    with POOL.lock(target['id']):
        client, _ = POOL.get(target)
    # Bandit B601/B108 reviewed: a fixed command; the path is on the node, a fresh
    # private directory from mktemp (0700), removed after the install
    _, out, _ = client.exec_command('d=$(mktemp -d /tmp/edgeshell-hb.XXXXXXXX) && chmod 700 "$d" && echo "$d"',  # nosec B601 B108
                                    timeout=15)
    remote = out.read().decode().strip()
    # Bandit B108 reviewed: checks the node-side mktemp result, not a local temp file
    if not remote.startswith('/tmp/edgeshell-hb.'):  # nosec B108
        raise OSError('could not create a staging directory on the node')
    sftp = client.open_sftp()
    try:
        for name, text in files.items():
            path = posixpath.join(remote, name)
            with sftp.open(path, 'w') as fh:
                fh.write(text)
            sftp.chmod(path, 0o600)
    finally:
        sftp.close()
    return remote


def _read(name):
    with open(os.path.join(AGENT_DIR, name), encoding='utf-8') as fh:
        return fh.read()


def install(node):
    """Install or update the agent on one node and push the fleet config.yaml.
    Returns (ok, output)."""
    from app.services import collector
    if not enabled():
        return False, 'HEARTBEAT_MASTER_KEY is not set on this server'
    target = collector.target_for(node)
    s = agent_settings()
    files = {name: _read(name) for name in AGENT_FILES}
    files['token'] = join_token(node, s)              # this node's keys and the broker, never the master key
    files['login'] = login_env()
    try:
        d = _upload(target, files)
    except Exception as e:
        from app.services.errors import describe
        return False, describe(e, f'heartbeat upload to {node.name}')
    # setup.py tries the four ways to the broker (TLS first) with a real login,
    # keeps the best that works, and installs the hardened service.
    cmd = (f'sudo python3 {d}/setup.py --token-file {d}/token --login-file {d}/login '
           f'--interval {int(s["interval"])}; rc=$?; rm -rf {d}; exit $rc')
    r = collector.run_command(target, cmd, timeout=600, sudo_password=target['password'])
    out = (r.get('stdout') or '')[-3000:]
    ok = r.get('success') and 'HEARTBEAT_OK' in out and 'Done.' in out
    m = WAY.search(out)
    if m:
        node.hb_way = f'{m.group(1)}:{m.group(2)}'
    node.hb_installed = bool(ok) or node.hb_installed
    node.hb_checked_at = datetime.utcnow()
    db.session.commit()
    return bool(ok), out.strip() or (r.get('error') or '')


def test(node):
    """The agent's own check on the node: service, files, the four ways in, the login. (ok, output)"""
    from app.services import collector
    target = collector.target_for(node)
    cmd = (f'test -f {REMOTE_HOME}/setup.py || {{ echo NOT_INSTALLED_OR_OLD; exit 3; }}; '
           f'sudo python3 {REMOTE_HOME}/setup.py --test')
    r = collector.run_command(target, cmd, timeout=120, sudo_password=target['password'])
    out = r.get('stdout') or ''
    m = re.search(r'reached over (\S+)', out)
    if m:
        node.hb_way = m.group(1)
        db.session.commit()
    return 'HEARTBEAT_TEST ok' in out, out.strip()[-2000:] or (r.get('error') or '')


def push_login(node):
    """A new broker login on one node (checked against the broker first). (ok, message)"""
    from app.services import collector
    target = collector.target_for(node)
    try:
        d = _upload(target, {'login': login_env(), 'setup.py': _read('setup.py'),
                             'heartbeat_agent.py': _read('heartbeat_agent.py'),
                             'heartbeat_protocol.py': _read('heartbeat_protocol.py')})
    except Exception as e:
        from app.services.errors import describe
        return False, describe(e, f'heartbeat login push to {node.name}')
    cmd = f'sudo python3 {d}/setup.py --set-credentials --login-file {d}/login; rc=$?; rm -rf {d}; exit $rc'
    r = collector.run_command(target, cmd, timeout=120, sudo_password=target['password'])
    out = r.get('stdout') or ''
    return 'HEARTBEAT_LOGIN_OK' in out, (out.strip().splitlines() or [r.get('error') or 'no output'])[-1][:300]


def push_config(node, s=None):
    """Replace config.yaml on one node (the agent hot reloads it). (ok, message)"""
    from app.services import collector
    target = collector.target_for(node)
    try:
        d = _upload(target, {'config.yaml': config_yaml(s)})
    except Exception as e:
        from app.services.errors import describe
        return False, describe(e, f'heartbeat config push to {node.name}')
    cmd = (f'test -d {REMOTE_HOME} || {{ rm -rf {d}; echo NOT_INSTALLED; exit 3; }}; '
           f'if [ -f {REMOTE_HOME}/setup.py ]; then '      # 1.2+: merge, keep the way in this node found
           f'sudo python3 {REMOTE_HOME}/setup.py --merge-config {d}/config.yaml; '
           f'else sudo install -m 0644 -o root -g root {d}/config.yaml {REMOTE_HOME}/config.yaml.new && '
           f'sudo mv -f {REMOTE_HOME}/config.yaml.new {REMOTE_HOME}/config.yaml && echo CONFIG_PUSHED; fi; '
           f'rc=$?; rm -rf {d}; exit $rc')
    r = collector.run_command(target, cmd, timeout=60, sudo_password=target['password'])
    out = r.get('stdout') or ''
    if 'NOT_INSTALLED' in out:
        return False, 'agent not installed'
    return ('CONFIG_PUSHED' in out), ('pushed' if 'CONFIG_PUSHED' in out else (r.get('error') or out[-300:]))


CHECK = ('if [ -f /opt/edgeshell-heartbeat/heartbeat_agent.py ]; then echo HB_INSTALLED; fi; '
         '(systemctl is-active --quiet edgeshell-heartbeat 2>/dev/null || '
         'pgrep -u hbagent -f heartbeat_agent.py >/dev/null 2>&1) && echo HB_RUNNING')


def parse_check(stdout):
    """(installed, running) from the CHECK output."""
    return 'HB_INSTALLED' in (stdout or ''), 'HB_RUNNING' in (stdout or '')


# ------------------------------------------------------------------ SenseHive
def fetch_nodes():
    """SenseHive's per-node heartbeat state, or None when it is not reachable."""
    import requests
    cfg = current_app.config
    url = cfg['SENSEHIVE_URL'].rstrip('/') + '/api/heartbeat/nodes'
    r = requests.get(url, headers={'Authorization': f"Bearer {cfg['SENSEHIVE_API_TOKEN']}"}, timeout=8)
    r.raise_for_status()
    return r.json()


HISTORY_RANGES = ('1m', '1h', '1d', '7d', '30d')
EXPORT_KINDS = ('beats', 'minutes', 'events')
EXPORT_PERIODS = {'1h': 3600, '1d': 86400, '7d': 7 * 86400, '30d': 30 * 86400}


def _sensehive(path, params, timeout=15):
    """GET from SenseHive's read-only heartbeat API with EdgeShell's token. The
    address is EdgeShell's own setting, never anything a user sends."""
    import requests
    cfg = current_app.config
    return requests.get(cfg['SENSEHIVE_URL'].rstrip('/') + path, params=params, timeout=timeout,
                        headers={'Authorization': f"Bearer {cfg['SENSEHIVE_API_TOKEN']}"})


def fetch_history(name, rng):
    """Chart data for one node from SenseHive (1m to 30d), or None if it is not reachable.
    Raises LookupError when SenseHive does not know the node."""
    r = _sensehive('/api/heartbeat/history', {'node': name, 'range': rng})
    if r.status_code == 404:
        raise LookupError(name)
    r.raise_for_status()
    d = r.json()
    keep = ('node', 'range', 'since', 'until', 'kind', 'points', 'interval', 'breaks', 'received', 'lag_ms',
            'longest_break', 'current_break', 'events', 'uptime')
    return {k: d[k] for k in keep if k in d}


def fetch_export(names, what, seconds):
    """CSV text from SenseHive for some node names over the last `seconds`."""
    now = int(time.time())
    r = _sensehive('/api/heartbeat/export.csv', {'nodes': ','.join(names), 'what': what,
                                                   'since': now - seconds, 'until': now}, timeout=60)
    if r.status_code == 404:
        raise LookupError(','.join(names))
    r.raise_for_status()
    return r.text


def _dt(ts):
    return datetime.utcfromtimestamp(ts) if ts else None


def sync(nodes=None, data=None):
    """Copy SenseHive's heartbeat state onto the nodes (by node name) and add
    an event per change. Returns the number of nodes matched, or None."""
    from app.models.api import NodeEvent
    from app.models.node import Node
    if data is None:
        if not sensehive_configured():
            return None
        try:
            data = fetch_nodes()
        except Exception:
            current_app.logger.warning('heartbeat sync: SenseHive not reachable', exc_info=True)
            return None
    by_name = {n['name']: n for n in data.get('nodes', []) if isinstance(n, dict) and n.get('name')}
    nodes = nodes if nodes is not None else Node.query.filter_by(is_deleted=False).all()
    matched = 0
    for n in nodes:
        hb = by_name.get(n.name)
        if not hb:
            continue
        matched += 1
        status = hb.get('status') if hb.get('status') in ('online', 'offline', 'silent') else None
        if status != n.hb_status and n.hb_status is not None:
            db.session.add(NodeEvent(node_id=n.id, node_name=n.name, kind='heartbeat_changed',
                                     old_value=n.hb_status, new_value=status,
                                     message='last will received' if status == 'offline' else None))
        n.hb_status = status
        n.hb_last_seen, n.hb_since, n.hb_lwt_at = _dt(hb.get('last_seen')), _dt(hb.get('since')), _dt(hb.get('lwt_at'))
        n.hb_agent_version = (hb.get('agent_version') or '')[:16] or None
        if status:
            n.hb_installed = True
    db.session.commit()
    return matched


def fresh(node, now=None):
    """A verified heartbeat in the last FRESH_S seconds."""
    if node.hb_status != 'online' or not node.hb_last_seen:
        return False
    now = now or datetime.utcnow()
    return (now - node.hb_last_seen).total_seconds() <= FRESH_S


class Syncer:
    """Throttled sync, called from the collector loop."""

    def __init__(self):
        self.last = 0

    def tick(self):
        if time.time() - self.last < SYNC_EVERY_S:
            return
        self.last = time.time()
        sync()


# ------------------------------------------------------------------ the routine (ADR 0016)
# Every node should send heartbeats: they are how the SLA tells a node that is
# down from a VPN or SSH glitch. The routine keeps the agent installed, current
# and working on every node SSH reaches, by itself.
ROUTINE_KEY = 'heartbeat_routine'
ROUTINE_LAST_KEY = 'heartbeat_routine_last'
ROUTINE_DEFAULTS = {'enabled': True, 'hours': 6}
RETRY_AFTER_FAILURE_S = 24 * 3600   # a node that failed is left alone this long
MISSING_AFTER_S = 15 * 60           # SSH up but no heartbeat this long: repair


def routine_settings():
    from app.models import Settings
    out = dict(ROUTINE_DEFAULTS)
    row = Settings.query.filter_by(key=ROUTINE_KEY).first()
    if row and row.value:
        try:
            saved = json.loads(row.value)
            if isinstance(saved.get('enabled'), bool):
                out['enabled'] = saved['enabled']
            if isinstance(saved.get('hours'), int) and 1 <= saved['hours'] <= 168:
                out['hours'] = saved['hours']
        except ValueError:
            pass
    return out


def save_routine_settings(enabled, hours):
    from app.models import Settings
    if not isinstance(enabled, bool) or not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 168:
        raise ValueError('hours must be 1 to 168')
    value = json.dumps({'enabled': enabled, 'hours': hours})
    row = Settings.query.filter_by(key=ROUTINE_KEY).first()
    if row:
        row.value = value
    else:
        db.session.add(Settings(key=ROUTINE_KEY, value=value))
    db.session.commit()


def routine_last():
    from app.models import Settings
    row = Settings.query.filter_by(key=ROUTINE_LAST_KEY).first()
    try:
        return json.loads(row.value) if row and row.value else None
    except ValueError:
        return None


def _save_last(summary):
    from app.models import Settings
    row = Settings.query.filter_by(key=ROUTINE_LAST_KEY).first()
    value = json.dumps(summary, sort_keys=True)
    if row:
        row.value = value
    else:
        db.session.add(Settings(key=ROUTINE_LAST_KEY, value=value))
    db.session.commit()


def version_tuple(text):
    try:
        return tuple(int(x) for x in str(text).strip().lstrip('v').split('.')[:3])
    except (TypeError, ValueError):
        return None


def plan(node, now=None, shipped=None):
    """What the routine does for one node: 'install', 'update', 'repair' or None.
    Only for nodes SSH reaches now; a node that failed recently is left alone."""
    now = now or datetime.utcnow()
    if node.is_deleted or not node.is_active or node.status != 'online' or node.login_paused_at:
        return None
    if node.hb_routine_at and (node.hb_routine_note or '').startswith('failed') and \
            (now - node.hb_routine_at).total_seconds() < RETRY_AFTER_FAILURE_S:
        return None
    if not node.hb_installed:
        return 'install'
    have, ship = version_tuple(node.hb_agent_version), version_tuple(shipped or agent_version())
    if have and ship and have < ship:
        return 'update'
    if not fresh(node, now):
        seen = node.hb_last_seen
        if seen is None or (now - seen).total_seconds() >= MISSING_AFTER_S:
            return 'repair'
    return None


def run_routine_node(node, action):
    """One node: install, update or repair. Returns (ok, note) and records it on the node."""
    if action == 'repair':
        ok, out = test(node)
        if ok:
            note = 'checked: agent works on the node (waiting for SenseHive)'
        else:
            ok, out = install(node)
            note = ('repaired' if ok else 'failed repair') + ': ' + _gist(out)
    else:
        ok, out = install(node)
        note = (('installed' if action == 'install' else 'updated') if ok else f'failed {action}') + ': ' + _gist(out)
    node.hb_routine_at = datetime.utcnow()
    node.hb_routine_note = note[:200]                    # starts with "failed" when it did not work
    db.session.commit()
    return ok, note


def _gist(out):
    """The line that matters from setup.py's output."""
    lines = [x.strip() for x in (out or '').splitlines() if x.strip()]
    for prefix in ('Not set up', 'Broker:', 'HEARTBEAT_TEST', 'FAIL'):
        hit = next((x for x in lines if x.startswith(prefix)), None)
        if hit:
            return hit[:160]
    return (lines[-1] if lines else 'no output')[:160]


def run_routine(owner_id=None, actor='routine'):
    """Plan and start one pass over the fleet as a background job. Returns
    (job id or None, plan summary)."""
    from app.models.node import Node
    from app.services import jobs
    from app.services.activity_service import log_activity
    sync()
    now, shipped = datetime.utcnow(), agent_version()
    todo = []
    for n in Node.query.filter_by(is_deleted=False).all():
        action = plan(n, now, shipped)
        if action:
            todo.append((n.id, action))
    limit = int(current_app.config.get('HEARTBEAT_ROUTINE_MAX') or 25)
    counts = {a: sum(1 for _, x in todo if x == a) for a in ('install', 'update', 'repair')}
    summary = {'at': now.isoformat() + 'Z', 'planned': counts, 'deferred': max(0, len(todo) - limit),
               'shipped_version': shipped, 'by': actor}
    todo = todo[:limit]
    if not todo:
        summary['result'] = 'every reachable node is covered'
        _save_last(summary)
        return None, summary

    def work(item):
        nid, action = item
        node = db.session.get(Node, nid)
        if node is None:
            return False
        ok, note = run_routine_node(node, action)
        log_activity(f'Heartbeat routine: {action}', node=node.name, status='success' if ok else 'failure',
                     response=note, user=actor)
        return ok

    def done(job):
        summary.update(result=f"{job['ok']} ok, {job['failed']} failed", ok=job['ok'], failed=job['failed'])
        _save_last(summary)
        return summary['result']
    jid = jobs.run_each('heartbeat-routine', f'Heartbeat routine: {len(todo)} node(s)', owner_id, todo, work,
                        workers=8, summary=done)
    return jid, summary


class Keeper:
    """Starts the routine from the collector loop every `hours` (Fleet Setup)."""

    FIRST_AFTER_S = 600      # the first pass about 10 minutes after EdgeShell starts

    def __init__(self):
        self.started, self.last = time.time(), None

    def tick(self):
        s = routine_settings()
        if not s['enabled'] or not enabled():
            return
        now = time.time()
        due = (now - self.started >= self.FIRST_AFTER_S) if self.last is None else (now - self.last >= s['hours'] * 3600)
        if not due:
            return
        self.last = time.time()
        try:
            run_routine()
        except Exception:                                # never break the collector loop
            current_app.logger.warning('heartbeat routine failed', exc_info=True)


KEEPER = Keeper()
