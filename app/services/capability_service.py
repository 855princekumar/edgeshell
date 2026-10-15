"""Fleet capability scan and install for Docker, Node-RED, OpenRemoteGUI and
the MQTT heartbeat agent.

One SSH round-trip reports which of the three are present on a node. Install
runs the documented one-liner per capability. Docker uses sudo for the running
check because the login user is often not in the docker group.
"""
from datetime import datetime
from flask import current_app
from app import db
from app.services.ssh_service import execute_command
from app.services import orgui_service

def _check_cmd(orgui_port):
    """Build the per-node capability probe.

    OpenRemoteGUI counts only when installed and its gateway port listens
    (see orgui_service): Raspberry Pi OS's own wayvnc is not OpenRemoteGUI.
    Also reports arch.
    """
    return (
        'echo "ARCH:$(uname -m)"; '
        'echo "DOCKER:$(command -v docker >/dev/null 2>&1 && echo 1 || echo 0)"; '
        'echo "NODERED:$((command -v node-red >/dev/null 2>&1 || '
        "systemctl list-unit-files 2>/dev/null | grep -qE 'nodered|node-red') && echo 1 || echo 0)\"; "
        'echo "ORGUI:$(' + orgui_service._CHECK.format(port=int(orgui_port))
        + ' | grep -q RUNNING && echo 1 || echo 0)"; '
        'echo "HEARTBEAT:$((' + _hb_check() + ') | grep -q HB_RUNNING && echo 1 || echo 0)"'
    )


def _hb_check():
    from app.services.heartbeat_service import CHECK
    return CHECK


CAPABILITIES = ('docker', 'nodered', 'orgui', 'heartbeat')
# The node column for each capability.
COLUMNS = {'docker': 'docker_installed', 'nodered': 'nodered_installed', 'orgui': 'orgui_installed',
           'heartbeat': 'hb_installed'}


def check_node(node):
    """Return dict {docker, nodered, orgui, arch}, or None if unreachable."""
    port = node.orgui_port or current_app.config.get('ORGUI_DEFAULT_PORT', 6080)
    r = execute_command(node, _check_cmd(port), timeout=20)
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


def scan_fleet(nodes):
    per_node = []
    counts = {c: {'installed': 0, 'missing': 0, 'unknown': 0} for c in CAPABILITIES}
    for n in nodes:
        state = check_node(n)
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
