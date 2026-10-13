"""Interactive SSH terminal for one node, over a WebSocket.

The browser runs xterm.js; this bridges it to an interactive shell on the
node (paramiko invoke_shell with a real PTY), so it behaves like `ssh user@node`:
colors, full-screen programs (top, nano, vim), tab completion, Ctrl+C, and
sudo prompting for the password like any terminal.

Messages from the browser are JSON: {"t": "i", "d": "<keystrokes>"} and
{"t": "r", "c": <cols>, "r": <rows>}. The node's output is sent as text.

Safety: signed-in operators and administrators only; the WebSocket must come
from EdgeShell's own page (Origin check, against cross-site WebSocket use);
the session closes when the window closes, after 30 minutes without input, or
as soon as the EdgeShell sign-in ends. Opening and closing are audited.
"""
import codecs
import json
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import paramiko
from flask import request, session
from flask_login import current_user

from app import db, sock
from app.services import collector
from app.services.activity_service import log_activity

MAX_SESSIONS = 12
PER_USER = 4
IDLE_LIMIT_S = 30 * 60
CHECK_SIGNIN_EVERY_S = 20

_active = {}
_guard = threading.Lock()


def _same_origin():
    """The WebSocket must come from EdgeShell's own page. Hostnames are
    compared (a cross-site page always differs there); the port is left out
    because some clients and proxies drop it from the Host header."""
    origin = request.headers.get('Origin', '')
    if not origin:
        return True
    host = request.host.rsplit(':', 1)[0] if not request.host.endswith(']') else request.host
    return (urlparse(origin).hostname or '').lower() == host.strip('[]').lower()


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _signed_in(sid):
    from app.models.user import UserSession
    from app.services.session_service import idle_limits
    row = UserSession.query.filter_by(token=sid).first()
    db.session.remove()
    if not row or row.revoked_at is not None:
        return False
    idle, warn = idle_limits()
    last = row.active_at or row.started_at
    return not idle or datetime.utcnow() - last <= timedelta(seconds=idle + warn)


@sock.route('/ws/nodes/<int:node_id>/shell')
def node_shell(ws, node_id):
    from app.models.node import Node
    if not current_user.is_authenticated or not current_user.can_execute() or not _same_origin():
        ws.send('\r\n\x1b[31mNot allowed: operators and administrators only.\x1b[0m\r\n')
        return
    node = db.session.get(Node, node_id)
    if not node or not node.is_active:
        ws.send('\r\n\x1b[31mNode not found.\x1b[0m\r\n')
        return
    target = collector.target_for(node)
    user_id, username, sid = current_user.id, current_user.username, session.get('sid')
    db.session.remove()
    with _guard:
        mine = sum(1 for s in _active.values() if s[0] == user_id)
        if len(_active) >= MAX_SESSIONS or mine >= PER_USER:
            ws.send(f'\r\n\x1b[31mToo many open terminals (at most {PER_USER} per user and '
                    f'{MAX_SESSIONS} in total). Close one and try again.\x1b[0m\r\n')
            return
        key = object()
        _active[key] = (user_id, target['name'])

    cols = _clamp(request.args.get('cols'), 20, 500, 120)
    rows = _clamp(request.args.get('rows'), 5, 200, 32)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    started = time.time()
    chan = None
    try:
        ws.send(f"\x1b[90mConnecting to {target['username']}@{target['ip']}:{target['port']}...\x1b[0m\r\n")
        try:
            client.connect(target['ip'], port=target['port'], username=target['username'],
                           password=target['password'], timeout=10, banner_timeout=10,
                           auth_timeout=10, allow_agent=False, look_for_keys=False)
        except Exception as e:
            kind, message = collector.classify(e)
            ws.send(f'\r\n\x1b[31m{message}\x1b[0m\r\n')
            return
        client.get_transport().set_keepalive(20)
        chan = client.invoke_shell(term='xterm-256color', width=cols, height=rows)
        log_activity('Terminal session opened', node=target['name'], status='success')

        def pump():
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            try:
                while True:
                    data = chan.recv(32768)
                    if not data:
                        break
                    text = decoder.decode(data)
                    if text:
                        ws.send(text)
            except Exception:
                pass
            finally:
                try:
                    ws.send('\r\n\x1b[90m[session ended]\x1b[0m\r\n')
                    ws.close()
                except Exception:
                    pass

        reader = threading.Thread(target=pump, name=f'shell-{node_id}', daemon=True)
        reader.start()
        last_input = last_check = time.time()
        while reader.is_alive():
            msg = ws.receive(timeout=1)
            now = time.time()
            if msg is not None:
                try:
                    m = json.loads(msg)
                except ValueError:
                    m = {}
                if m.get('t') == 'i' and isinstance(m.get('d'), str):
                    chan.send(m['d'])
                    last_input = now
                elif m.get('t') == 'r':
                    chan.resize_pty(width=_clamp(m.get('c'), 20, 500, cols),
                                    height=_clamp(m.get('r'), 5, 200, rows))
            if now - last_input > IDLE_LIMIT_S:
                ws.send('\r\n\x1b[33mClosed after 30 minutes without input.\x1b[0m\r\n')
                break
            if now - last_check > CHECK_SIGNIN_EVERY_S:
                last_check = now
                if sid and not _signed_in(sid):
                    ws.send('\r\n\x1b[33mYour EdgeShell session ended, so this terminal was closed.\x1b[0m\r\n')
                    break
    except Exception:
        pass                                   # browser went away: just clean up
    finally:
        if chan is not None:
            try:
                chan.close()
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass
        with _guard:
            _active.pop(key, None)
        if chan is not None:
            try:
                log_activity('Terminal session closed', node=target['name'], status='success',
                             duration=round(time.time() - started, 1),
                             response=f'{username}, {round(time.time() - started)} s')
            except Exception:
                db.session.rollback()
