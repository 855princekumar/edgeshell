"""Fleet capability scan and install for Docker, Node-RED, OpenRemoteGUI and
the MQTT heartbeat agent.

One SSH round-trip per node reports the capabilities asked for (one, or all).
Results are stored on the node, so Fleet Setup opens instantly from them
(summary) and each capability is re-checked on its own, in a background job.
Install runs the documented one-liner per capability.
"""
from datetime import datetime
from flask import current_app
from app import db
from app.services.ssh_service import execute_command
from app.services import orgui_service

def _probe(cap, orgui_port):
    if cap == 'docker':
        return 'echo "DOCKER:$(command -v docker >/dev/null 2>&1 && echo 1 || echo 0)"'
    if cap == 'nodered':
        return ('echo "NODERED:$((command -v node-red >/dev/null 2>&1 || '
                "systemctl list-unit-files 2>/dev/null | grep -qE 'nodered|node-red') && echo 1 || echo 0)\"")
    if cap == 'orgui':
        return ('echo "ORGUI:$(' + orgui_service._CHECK.format(port=int(orgui_port))
                + ' | grep -q RUNNING && echo 1 || echo 0)"')
    return 'echo "HEARTBEAT:$((' + _hb_check() + ') | grep -q HB_RUNNING && echo 1 || echo 0)"'


def _check_cmd(orgui_port, caps=None):
    """Build the per-node probe for `caps` (default: all), plus the arch.

    OpenRemoteGUI counts only when installed and its gateway port listens
    (see orgui_service): Raspberry Pi OS's own wayvnc is not OpenRemoteGUI.
    """
    return '; '.join(['echo "ARCH:$(uname -m)"'] + [_probe(c, orgui_port) for c in (caps or CAPABILITIES)])


def _hb_check():
    from app.services.heartbeat_service import CHECK
    return CHECK


CAPABILITIES = ('docker', 'nodered', 'orgui', 'heartbeat')
# The node column for each capability.
COLUMNS = {'docker': 'docker_installed', 'nodered': 'nodered_installed', 'orgui': 'orgui_installed',
           'heartbeat': 'hb_installed'}


def check_node(node, caps=None):
    """Return dict {<capability>: bool, arch}, or None if unreachable."""
    port = node.orgui_port or current_app.config.get('ORGUI_DEFAULT_PORT', 6080)
    r = execute_command(node, _check_cmd(port, caps), timeout=20)
    if not r.get('success') and not r.get('stdout'):
        return None
    out = {}
    for line in (r.get('stdout') or '').splitlines():
        line = line.strip()
        if line.startswith('ARCH:'):
            out['arch'] = line.split(':', 1)[1].strip()
        for key, cap in (('DOCKER:', 'docker'), ('NODERED:', 'nodered'), ('ORGUI:', 'orgui'),
                         ('HEARTBEAT:', 'heartbeat')):
            if line.startswith(key):
                out[cap] = line.split(':', 1)[1].strip() == '1'
    return out or None


def scan_fleet(nodes, workers=16):
    """Check every node (16 at a time over SSH), then record the results."""
    from concurrent.futures import ThreadPoolExecutor
    app = current_app._get_current_object()

    def probe(n):
        with app.app_context():
            try:
                return check_node(n)
            except Exception:
                return None
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(nodes) or 1))) as pool:
        states = list(pool.map(probe, nodes))
    per_node = []
    counts = {c: {'installed': 0, 'missing': 0, 'unknown': 0} for c in CAPABILITIES}
    for n, state in zip(nodes, states):
        if state is not None:
            n.docker_installed = state.get('docker')
            n.nodered_installed = state.get('nodered')
            n.orgui_installed = state.get('orgui')
            if 'heartbeat' in state:
                n.hb_installed = state.get('heartbeat')
                n.hb_checked_at = datetime.utcnow()
            if state.get('arch'):
                n.arch = state['arch']
            n.capabilities_checked_at = datetime.utcnow()
        row = {'id': n.id, 'name': n.name, 'ip': n.ip_address,
               'arch': (state or {}).get('arch')}
        for c in CAPABILITIES:
            val = None if state is None else state.get(c)
            row[c] = val
            bucket = 'installed' if val is True else ('missing' if val is False else 'unknown')
            counts[c][bucket] += 1
        per_node.append(row)
    db.session.commit()
    return {'total': len(nodes), 'counts': counts, 'nodes': per_node}


def record(node, state):
    """Store a probe result on the node (only the capabilities it reports)."""
    if state is None:
        return
    for cap in CAPABILITIES:
        if cap in state:
            setattr(node, COLUMNS[cap], state[cap])
    if 'heartbeat' in state:
        node.hb_checked_at = datetime.utcnow()
    if state.get('arch'):
        node.arch = state['arch']
    node.capabilities_checked_at = datetime.utcnow()


def summary(nodes):
    """Fleet Setup from what is stored (no SSH): counts and per-node state."""
    counts = {c: {'installed': 0, 'missing': 0, 'unknown': 0} for c in CAPABILITIES}
    rows = []
    for n in nodes:
        row = {'id': n.id, 'name': n.name, 'ip': n.ip_address, 'arch': n.arch, 'status': n.status}
        for c in CAPABILITIES:
            val = getattr(n, COLUMNS[c])
            if c == 'heartbeat' and n.hb_status in ('online', 'offline', 'silent'):
                val = True                         # SenseHive hears it: the agent is there
            row[c] = val
            counts[c]['installed' if val is True else ('missing' if val is False else 'unknown')] += 1
        rows.append(row)
    checked = [n.capabilities_checked_at for n in nodes if n.capabilities_checked_at]
    from app.utils.timeutil import iso_utc
    return {'total': len(nodes), 'counts': counts, 'nodes': rows,
            'checked_at': iso_utc(max(checked)) if checked else None}


def _install_command(capability):
    repo = current_app.config['ORGUI_REPO']
    if capability == 'docker':
        return ('curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && '
                'sudo sh /tmp/get-docker.sh && sudo usermod -aG docker "$USER"')
    if capability == 'nodered':
        return ('curl -sL https://raw.githubusercontent.com/node-red/'
                'linux-installers/master/deb/update-nodejs-and-nodered -o /tmp/nr.sh && '
                'bash /tmp/nr.sh --confirm-install --confirm-pi --nodered-user="$USER"')
    if capability == 'orgui':
        return ('cd "$HOME"; if [ ! -d OpenRemoteGUI ]; then git clone %s; fi; '
                'cd OpenRemoteGUI && sudo bash install.sh' % repo)
    return None


def install(node, capability):
    """Install one capability on one node. Returns (success, output)."""
    if capability == 'heartbeat':
        from app.services import heartbeat_service
        return heartbeat_service.install(node)
    cmd = _install_command(capability)
    if not cmd:
        return False, 'unknown capability'
    timeout = 900 if capability in ('docker', 'nodered') else 600
    r = execute_command(node, cmd, timeout=timeout)
    ok = r.get('success', False)
    if ok:
        setattr(node, COLUMNS[capability], True)
        node.capabilities_checked_at = datetime.utcnow()
        db.session.commit()
    return ok, (r.get('stdout', '') + r.get('stderr', '')).strip()[-4000:]
