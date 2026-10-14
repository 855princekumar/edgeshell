"""Node login policy: stop logging in to a node whose login keeps failing.

Repeating a password a node has rejected achieves nothing and looks like
password guessing to the node (fail2ban bans, account lockouts, alerts), so:

- Pause. After LOGIN_PAUSE_AFTER_FAILURES password failures in a row (or one
  refusal by Tailscale SSH, which ignores passwords and waits for a browser
  check), automatic logins to that node stop. The pause is stored on the node,
  so a restart does not start a new round of failed logins. Bulk actions
  (Refresh All, Test All, fleet terminal, file deploy, scans and installs)
  skip paused nodes; every SSH login goes through ssh_service, which refuses
  them.
- Resume by a person: Fix login (new credentials), Test on the node's card
  (one attempt), or "Retry paused logins".
- Resume by itself, without guessing: every LOGIN_RECHECK_MINUTES the SSH
  banner of each paused node is read (a TCP read, no login, no password). A
  changed banner means the SSH server changed (for example Tailscale SSH
  turned off), so the node gets one new attempt. For nodes paused by Tailscale
  SSH, one of them is tried (a single "canary" login for the whole group);
  when it works the tailnet policy was fixed and they all resume.
- Every pause and resume is in the activity log.
"""
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import current_app

from app import db

REASONS = {
    'password': 'the node rejected the SSH username or password',
    'tailscale': 'Tailscale SSH on the node asks for a browser check',
    'unknown': 'logins were failing before this policy started',
}


def _cfg(name, default):
    try:
        return int(current_app.config.get(name, default))
    except (TypeError, ValueError):
        return default


def read_banner(ip, port, timeout=5):
    """The SSH server's identification line, read without logging in."""
    s = None
    try:
        s = socket.create_connection((ip, port or 22), timeout=timeout)
        data = b''
        while b'\n' not in data and len(data) < 256:
            chunk = s.recv(256)
            if not chunk:
                break
            data += chunk
        line = data.split(b'\n')[0].decode('latin1').strip()
        return line[:80] if line.startswith('SSH-') else None
    except OSError:
        return None
    finally:
        if s is not None:
            s.close()


def pause(node, reason, banner=None, message=None):
    node.login_paused_at = datetime.utcnow()
    node.login_pause_reason = reason
    if banner:
        node.ssh_banner = banner[:80]
    node.status = 'auth_failed'
    note = ' Automatic logins are paused.'
    node.last_error = ((message or REASONS.get(reason, '')) + note)[:250]


def resume(node):
    node.login_paused_at = None
    node.login_pause_reason = None


def apply(nodes, results, collector):
    """After a collection: pause nodes that keep failing, resume those that work."""
    from app.services.activity_service import log_activity
    limit = max(1, _cfg('LOGIN_PAUSE_AFTER_FAILURES', 2))
    by_id = {n.id: n for n in nodes}
    events = []
    for nid, r in results.items():
        n = by_id.get(nid)
        if n is None:
            continue
        if r.get('ok'):
            collector.auth_fails.pop(nid, None)
            if n.login_paused_at is not None:
                resume(n)
                events.append(('Logins resumed', n.name, 'login works again', 'success'))
            continue
        if r.get('kind') != 'auth' or n.login_paused_at is not None:
            continue
        count = collector.auth_fails.get(nid, 0) + 1
        collector.auth_fails[nid] = count
        reason = r.get('reason') or 'password'
        if reason == 'tailscale' or count >= limit:
            pause(n, reason, r.get('banner'), r.get('message'))
            events.append(('Logins paused', n.name,
                           f'{REASONS[reason]} ({count} failed login(s)); no more automatic logins',
                           'warning'))
    if events:
        _feed(events, by_name={n.name: n for n in nodes})
        db.session.commit()
        for action, name, text, status in events:
            log_activity(action, node=name, response=text, status=status)


def _feed(events, by_name):
    """Add pause and resume events to the API event feed."""
    from app.models.api import NodeEvent
    for action, name, text, _ in events:
        n = by_name.get(name)
        db.session.add(NodeEvent(node_id=n.id if n else None, node_name=name,
                                 kind='login_paused' if action == 'Logins paused' else 'login_resumed',
                                 message=text[:250]))


def resume_nodes(node_ids=None, reason=None):
    """Clear the pause (Retry paused logins). Returns the number resumed."""
    from app.models.node import Node
    q = Node.query.filter(Node.login_paused_at.isnot(None), Node.is_deleted.isnot(True))
    if node_ids:
        q = q.filter(Node.id.in_(node_ids))
    if reason:
        q = q.filter(Node.login_pause_reason == reason)
    nodes = q.all()
    for n in nodes:
        resume(n)
    db.session.commit()
    return [n.id for n in nodes]


def recheck(collector, force=False):
    """Periodic check of paused nodes, without guessing passwords."""
    from app.models.node import Node
    from app.services import collector as col
    from app.services.activity_service import log_activity
    every = _cfg('LOGIN_RECHECK_MINUTES', 30) * 60
    if not force and (every <= 0 or time.time() - collector.last_login_recheck < every):
        return
    collector.last_login_recheck = time.time()
    paused = (Node.query.filter(Node.login_paused_at.isnot(None), Node.is_active.is_(True),
                                Node.is_deleted.isnot(True)).all())
    if not paused:
        return
    with ThreadPoolExecutor(max_workers=min(32, len(paused))) as pool:
        banners = dict(zip([n.id for n in paused],
                           pool.map(lambda n: read_banner(n.ip_address, n.ssh_port or 22), paused)))
    events, kick = [], []
    for n in paused:
        b = banners.get(n.id)
        if not b:
            continue                                   # not reachable now: keep waiting
        if n.login_pause_reason == 'unknown' or not n.ssh_banner:
            n.ssh_banner = b
            n.login_pause_reason = 'tailscale' if 'tailscale' in b.lower() else 'password'
            if n.login_pause_reason == 'tailscale':
                from app.services.ssh_service import TAILSCALE_CHECK_MESSAGE
                n.last_error = (TAILSCALE_CHECK_MESSAGE + ' Automatic logins are paused.')[:250]
            continue
        if b != n.ssh_banner:
            events.append(('Logins resumed', n.name, f'SSH server changed ({n.ssh_banner} to {b}); one new attempt',
                           'success'))
            n.ssh_banner = b
            resume(n)
            kick.append(n.id)
    db.session.commit()

    # One canary login for the whole group paused by Tailscale SSH.
    ts = [n for n in paused if n.login_paused_at is not None and n.login_pause_reason == 'tailscale']
    if ts:
        canary = min(ts, key=lambda n: collector.canary_at.get(n.id, 0))
        collector.canary_at[canary.id] = time.time()
        r = col.collect_one(col.target_for(canary))
        if r.get('ok'):
            ids = resume_nodes(reason='tailscale')
            kick += ids
            events.append(('Logins resumed', canary.name,
                           f'Tailscale SSH accepts EdgeShell again; {len(ids)} node(s) resumed', 'success'))
    if events:
        _feed(events, by_name={n.name: n for n in paused})
        db.session.commit()
    for action, name, text, status in events:
        log_activity(action, node=name, response=text, status=status)
    if kick:
        collector.kick(kick)
