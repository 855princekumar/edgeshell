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
  git clone <repo> && cd OpenRemoteGUI && sudo bash install.sh
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
        "cd OpenRemoteGUI && sudo bash install.sh"
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


# ---------------------------------------------------------------- guided install

REPO_URL = 'https://github.com/855princekumar/OpenRemoteGUI.git'
MIN_FREE_MB = 500

_PREFLIGHT = (
    'echo "OS:$(. /etc/os-release 2>/dev/null; echo "$ID $VERSION_ID")"; '
    'echo "FREE_MB:$(df -Pm "$HOME" | awk \'NR==2{print $4}\')"; '
    'echo "GIT:$(command -v git >/dev/null 2>&1 && echo 1 || echo 0)"; '
    'echo "NET:$( (curl -fsI --max-time 8 https://github.com >/dev/null 2>&1 || '
    'wget -q --spider -T 8 https://github.com >/dev/null 2>&1) && echo 1 || echo 0)"; '
    'echo "GUI:$( [ "$(systemctl get-default 2>/dev/null)" = graphical.target ] && echo 1 || echo 0)"'
)


def _parse(out):
    info = {}
    for line in out.splitlines():
        k, sep, v = line.strip().partition(':')
        if sep:
            info[k] = v.strip()
    return info


def preflight(target, sudo_password, run):
    """Checks that make the install safe to start. Returns (ok, problems, info)."""
    r = run(target, _PREFLIGHT, timeout=30, get_pty=False)
    if r['error']:
        return False, [r['error']], {}
    info = _parse(r['stdout'])
    problems = []
    os_id, _, version = info.get('OS', '').partition(' ')
    try:
        major = int(version.split('.')[0])
    except ValueError:
        major = 0
    if os_id not in ('debian', 'raspbian') or major < 12:
        problems.append(f"needs Debian or Raspberry Pi OS 12 (Bookworm) or newer, found '{info.get('OS') or 'unknown'}'")
    try:
        if int(info.get('FREE_MB', '0')) < MIN_FREE_MB:
            problems.append(f"needs {MIN_FREE_MB} MB free in the home folder, has {info.get('FREE_MB')} MB")
    except ValueError:
        problems.append('could not read the free disk space')
    if info.get('NET') != '1':
        problems.append('cannot reach github.com from the node')
    if info.get('GUI') != '1':
        problems.append('no desktop session (the system does not boot to graphical.target); '
                        'OpenRemoteGUI shares the desktop, so it needs Raspberry Pi OS with desktop')
    s = run(target, 'sudo true', timeout=30, sudo_password=sudo_password)
    if not s['success']:
        problems.append('sudo does not work for this login (wrong password, or the user is not allowed to use sudo)')
    return not problems, problems, info


def install_task(task, node_ids, stop_on_failure=True):
    """Guided install for one or more nodes, one at a time:
    preflight -> git clone (or update) -> cd + sudo bash install.sh -> verify."""
    import urllib.request
    from app.models.node import Node
    from app.services import collector
    from app.services.activity_service import log_activity

    run = collector.run_command
    nodes = Node.query.filter(Node.id.in_(node_ids)).all()
    for n in nodes:
        task.item(n.id, name=n.name, state='waiting', step='', message='')
    halted = False
    for n in nodes:
        if halted:
            task.item(n.id, state='skipped', message='not started: an earlier node failed')
            task.finish_item(False)
            continue
        t = collector.target_for(n)
        pw = t['password']

        def say(line, name=n.name):
            task.log(f'[{name}] {line}')

        task.item(n.id, state='running', step='Safety checks')
        say('Checking OS, disk space, network, desktop session and sudo...')
        ok, problems, info = preflight(t, pw, run)
        if not ok:
            for p in problems:
                say(f'  not safe to install: {p}')
            task.item(n.id, state='skipped', step='Safety checks', message='; '.join(problems))
            task.finish_item(False)
            continue
        say(f"  ok: {info.get('OS')}, {info.get('FREE_MB')} MB free")

        if info.get('GIT') != '1':
            task.item(n.id, step='Installing git')
            say('git is missing: installing it with apt...')
            r = run(t, 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git', timeout=300,
                    sudo_password=pw, on_output=say)
            if not r['success']:
                task.item(n.id, state='failed', message='could not install git')
                task.finish_item(False)
                halted = stop_on_failure
                continue

        task.item(n.id, step='Downloading (git clone)')
        say(f'git clone {REPO_URL}')
        r = run(t, 'cd "$HOME" && if [ -d OpenRemoteGUI/.git ]; then git -C OpenRemoteGUI pull --ff-only; '
                   f'else git clone --depth 1 {REPO_URL}; fi', timeout=300, on_output=say, get_pty=False)
        if not r['success']:
            task.item(n.id, state='failed', step='Downloading (git clone)', message=r['error'] or 'git clone failed')
            task.finish_item(False)
            halted = stop_on_failure
            continue

        task.item(n.id, step='Installing (sudo bash install.sh)')
        say('cd OpenRemoteGUI && sudo bash install.sh')
        r = run(t, 'cd "$HOME/OpenRemoteGUI" && sudo bash install.sh', timeout=1500, sudo_password=pw,
                on_output=say)
        if not r['success']:
            task.item(n.id, state='failed', step='Installing (sudo bash install.sh)',
                      message=r['error'] or f"install.sh exited with {r['exit_code']}")
            task.finish_item(False)
            log_activity('OpenRemoteGUI install', node=n.name, status='failure',
                         response=(r['stdout'] or '')[-1500:])
            halted = stop_on_failure
            continue

        task.item(n.id, step='Checking the web page')
        port = n.orgui_port or current_app.config.get('ORGUI_DEFAULT_PORT', 6080)
        state = run(t, _CHECK.format(port=port), timeout=30, get_pty=False)
        signal = (state['stdout'] or '').strip().splitlines()[-1:] or ['MISSING']
        page_ok = False
        try:
            # Bandit B310 reviewed: always an http:// URL to the node's own address and port
            with urllib.request.urlopen(f'http://{n.ip_address}:{port}/', timeout=10) as resp:  # nosec B310
                page_ok = resp.status == 200
        except Exception:
            pass
        node = db.session.get(Node, n.id)
        node.orgui_installed = signal[0] == 'RUNNING'
        node.orgui_checked_at = datetime.utcnow()
        db.session.commit()
        if signal[0] == 'RUNNING':
            where = f'http://{n.ip_address}:{port}'
            say(f'  done: OpenRemoteGUI is running at {where}'
                + ('' if page_ok else ' (this server cannot open the page itself; try it from your browser)'))
            task.item(n.id, state='done', step='Installed', message=where)
            task.finish_item(True)
            log_activity('OpenRemoteGUI install', node=n.name, status='success', response=where)
        else:
            say(f'  install finished but the web page is not running ({SIGNAL_TEXT.get(signal[0], signal[0])})')
            task.item(n.id, state='failed', step='Checking the web page',
                      message=SIGNAL_TEXT.get(signal[0], signal[0]))
            task.finish_item(False)
            halted = stop_on_failure
