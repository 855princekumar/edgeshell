"""OpenRemoteGUI lifecycle over SSH.

Detection strategy (first positive wins):
  1. systemd unit `openremotegui` is active
  2. the ORGUI port is listening
  3. the repo directory ~/OpenRemoteGUI exists

Install runs the documented one-liner:
  git clone <repo> && cd OpenRemoteGUI && sudo ./install.sh
"""
from datetime import datetime
from flask import current_app
from app import db
from app.services.ssh_service import execute_command

_CHECK = (
    "systemctl is-active openremotegui 2>/dev/null | grep -q '^active' && echo ACTIVE && exit 0; "
    "(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -qE ':{port}([^0-9]|$)' && echo PORT && exit 0; "
    "[ -d \"$HOME/noVNC\" ] && echo NOVNC && exit 0; "
    "pgrep -f 'websockify|wayvnc|novnc' >/dev/null 2>&1 && echo PROC && exit 0; "
    "[ -d \"$HOME/OpenRemoteGUI\" ] && echo REPO && exit 0; "
    "echo MISSING"
)


def check_installed(node):
    """Return (installed: bool|None, signal: str). None means the check failed."""
    port = node.orgui_port or current_app.config['ORGUI_DEFAULT_PORT']
    r = execute_command(node, _CHECK.format(port=port), timeout=20)
    if not r.get('success') and not r.get('stdout'):
        return None, r.get('stderr') or 'unreachable'
    out = (r.get('stdout') or '').strip().splitlines()
    signal = out[-1].strip() if out else 'MISSING'
    return (signal in ('ACTIVE', 'PORT', 'REPO', 'NOVNC', 'PROC')), signal


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
