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
import json
import os
import posixpath
import time
from datetime import datetime

from flask import current_app

from app import db

AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agents', 'heartbeat')
AGENT_FILES = ('heartbeat_agent.py', 'heartbeat_protocol.py', 'install.sh')
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
    files = {name: _read(name) for name in AGENT_FILES}
    files['agent.env'] = agent_env(node)
    files['config.yaml'] = config_yaml()
    try:
        d = _upload(target, files)
    except Exception as e:
        from app.services.errors import describe
        return False, describe(e, f'heartbeat upload to {node.name}')
    # The pushed config.yaml goes in first (atomically), so the agent starts
    # with this fleet's broker; install.sh never overwrites an existing one.
    cmd = (f'sudo mkdir -p {REMOTE_HOME} && '
           f'sudo install -m 0644 -o root -g root {d}/config.yaml {REMOTE_HOME}/config.yaml.new && '
           f'sudo mv -f {REMOTE_HOME}/config.yaml.new {REMOTE_HOME}/config.yaml && '
           f'sudo bash {d}/install.sh {d}/agent.env; rc=$?; rm -rf {d}; exit $rc')
    r = collector.run_command(target, cmd, timeout=600, sudo_password=target['password'])
    out = (r.get('stdout') or '')[-3000:]
    ok = r.get('success') and 'HEARTBEAT_OK' in out
    node.hb_installed = bool(ok) or node.hb_installed
    node.hb_checked_at = datetime.utcnow()
    db.session.commit()
    return bool(ok), out.strip() or (r.get('error') or '')


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
           f'sudo install -m 0644 -o root -g root {d}/config.yaml {REMOTE_HOME}/config.yaml.new && '
           f'sudo mv -f {REMOTE_HOME}/config.yaml.new {REMOTE_HOME}/config.yaml && echo CONFIG_PUSHED; '
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
            'longest_break', 'events', 'uptime')
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
