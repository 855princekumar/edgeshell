"""OpenRemoteGUI lifecycle over SSH.

Detection: OpenRemoteGUI counts as available only when its install
(/opt/openremotegui, /etc/openremotegui, or a ~/OpenRemoteGUI checkout) is
present AND its web gateway port (6080 by default) is listening.

  RUNNING     installed and the gateway port is listening: usable
  STOPPED     installed, but nothing listens on the port
  PORT_OTHER  something else listens on the port; OpenRemoteGUI is not installed
  MISSING     not installed

Process names are deliberately not used: Raspberry Pi OS runs its own
wayvnc (and Raspberry Pi Connect's rpi-connectd) on every node with VNC
enabled, which made every reachable Pi look like it had OpenRemoteGUI. And
`pgrep -f` also matches the checking shell's own command line.

Install runs the documented one-liner:
  git clone <repo> && cd OpenRemoteGUI && sudo ./install.sh
"""
from datetime import datetime
from flask import current_app
from app import db
from app.services.ssh_service import execute_command

_CHECK = (
    'd=0; ( [ -d /opt/openremotegui ] || [ -d /etc/openremotegui ] || [ -d "$HOME/OpenRemoteGUI" ] ) && d=1; p=0; (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -qE \'[:.]{port}([^0-9]|$)\' && p=1; if [ $d = 1 ] && [ $p = 1 ]; then echo RUNNING; elif [ $d = 1 ]; then echo STOPPED; elif [ $p = 1 ]; then echo PORT_OTHER; else echo MISSING; fi'
)

SIGNAL_TEXT = {
    'RUNNING': 'running',
    'STOPPED': 'installed but not running (start it on the node)',
    'PORT_OTHER': 'not installed (another program uses the port)',
    'MISSING': 'not installed',
}


def check_installed(node):
    """Return (installed: bool|None, signal: str). None means the check failed."""
    port = node.orgui_port or current_app.config['ORGUI_DEFAULT_PORT']
    r = execute_command(node, _CHECK.format(port=port), timeout=20)
    if not r.get('success') and not r.get('stdout'):
        return None, r.get('stderr') or 'unreachable'
    out = (r.get('stdout') or '').strip().splitlines()
    signal = out[-1].strip() if out else 'MISSING'
    return signal == 'RUNNING', signal


def persist_check(node):
    installed, signal = check_installed(node)
    node.orgui_installed = installed
    node.orgui_checked_at = datetime.utcnow()
    db.session.commit()
    return installed, signal


def install(node):
    """Install ORGUI on a single node. Returns (success, output)."""
    repo = current_app.config['ORGUI_REPO']
    cmd = (
        "set -e; cd \"$HOME\"; "
        "if [ ! -d OpenRemoteGUI ]; then git clone {repo}; fi; "
        "cd OpenRemoteGUI && sudo ./install.sh"
    ).format(repo=repo)
    r = execute_command(node, cmd, timeout=600)
    ok = r.get('success', False)
    if ok:
        node.orgui_installed = True
        node.orgui_checked_at = datetime.utcnow()
        db.session.commit()
    return ok, (r.get('stdout', '') + r.get('stderr', '')).strip()


def scan_fleet(nodes):
    """Check ORGUI presence across many nodes. Returns a summary dict."""
    per_node, installed, missing, unknown = [], 0, 0, 0
    for n in nodes:
        state, signal = persist_check(n)
        if state is True:
            installed += 1
        elif state is False:
            missing += 1
        else:
            unknown += 1
        per_node.append({'id': n.id, 'name': n.name, 'ip': n.ip_address,
                         'installed': state, 'signal': signal,
                         'orgui_port': n.orgui_port or 6080})
    return {'total': len(nodes), 'installed': installed,
            'missing': missing, 'unknown': unknown, 'nodes': per_node}
