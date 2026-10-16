"""Node apps on EdgeShell's own address (ADR 0014): every node's Node-RED at
/nodered/<node>/ and its remote desktop (OpenRemoteGUI, noVNC) at
/remoteGUI/<node>/.

nginx proxies those paths to the node after asking /internal/nodered-check or
/internal/remotegui-check, which allow signed-in operators and administrators
and return the node's address in a header nginx keeps to itself. The browser
never sees the node's IP or port, never needs the VPN, and the node apps never
see EdgeShell's cookies. The remote desktop's WebSocket (keyboard and mouse on
the node) also needs a ticket issued by the Remote button.
"""
import re
from urllib.parse import parse_qs, quote, urlsplit

from flask import Blueprint, current_app, redirect, request, session
from flask_login import current_user, login_required
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.models.node import Node
from app.services import isolation
from app.services.activity_service import log_activity

nodered_bp = Blueprint('nodered', __name__)

SAFE = re.compile(r'[A-Za-z0-9._-]{1,100}')
HOST = re.compile(r'[A-Za-z0-9.-]{1,253}')            # IPv4 or a DNS name; nothing nginx could read as a path
DESKTOP_TICKET_SECONDS = 120


def segment_for(node):
    """The node's part of the address: its name when it is URL-safe, else node-<id>."""
    return node.name if SAFE.fullmatch(node.name or '') else f'node-{node.id}'


def _node_from(segment):
    if not SAFE.fullmatch(segment or ''):
        return None
    q = Node.query.filter(Node.is_deleted.isnot(True), Node.is_active.is_(True))
    node = q.filter(Node.name == segment).first()
    if node is None and segment.startswith('node-') and segment[5:].isdigit():
        node = q.filter(Node.id == int(segment[5:])).first()
        if node is not None and segment_for(node) != segment:
            node = None                                  # node-<id> only for names that need it
    return node


def _tickets():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='edgeshell-desktop-ticket')


def desktop_ticket(user_id, node_id, sid):
    return _tickets().dumps({'u': user_id, 'n': node_id, 's': sid})


def desktop_ticket_ok(ticket, user_id, node_id, sid):
    try:
        data = _tickets().loads(ticket or '', max_age=DESKTOP_TICKET_SECONDS)
    except BadSignature:
        return False
    return data == {'u': user_id, 'n': node_id, 's': sid}


def _upstream(node, port):
    if node is None or not HOST.fullmatch(node.ip_address or ''):
        return None                                      # only a plain host or IP ever reaches nginx
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    return f'{node.ip_address}:{port}' if 1 <= port <= 65535 else None


def _check(prefix, default_port_of, ticket_for_websocket=False):
    """Shared nginx auth_request answer: 204 with X-Node-Upstream, 401 when signed
    out, 403 otherwise. Not reachable from outside: nginx blocks /internal/."""
    uri = request.headers.get('X-Original-URI', '')
    m = re.match(r'^' + re.escape(prefix) + r'([^/?#]+)', uri)
    if not m:
        return '', 403
    if not current_user.is_authenticated:
        return '', 401
    if not current_user.can_execute() or isolation.app_reason(request, prefix, m.group(1)):
        return '', 403
    node = _node_from(m.group(1))
    up = _upstream(node, default_port_of(node)) if node else None
    if up is None:
        return '', 403
    if ticket_for_websocket and request.headers.get('X-Original-Upgrade', '').lower() == 'websocket':
        ticket = (parse_qs(urlsplit(uri).query).get('ticket') or [''])[0]
        if not desktop_ticket_ok(ticket, current_user.id, node.id, session.get('sid')):
            return '', 403                               # keyboard and mouse only from EdgeShell's Remote button
    return '', 204, {'X-Node-Upstream': up, 'X-NodeRed-Upstream': up}


@nodered_bp.route('/nodes/<int:node_id>/nodered')
@login_required
def open_nodered(node_id):
    """The Node-RED button: logs the opening and goes to /nodered/<node>/."""
    if not current_user.can_execute():
        return 'Opening Node-RED needs the operator role.', 403
    node = Node.query.filter(Node.id == node_id, Node.is_deleted.isnot(True)).first_or_404()
    log_activity('Open Node-RED', node=node.name, status='success')
    return redirect(f'/nodered/{segment_for(node)}/')


@nodered_bp.route('/nodes/<int:node_id>/remotegui')
@login_required
def open_remotegui(node_id):
    """The Remote button: logs the opening and opens the node's noVNC client at
    /remoteGUI/<node>/, its WebSocket on the same path with a fresh ticket."""
    if not current_user.can_execute():
        return 'The remote desktop needs the operator role.', 403
    node = Node.query.filter(Node.id == node_id, Node.is_deleted.isnot(True)).first_or_404()
    seg = segment_for(node)
    ticket = desktop_ticket(current_user.id, node.id, session.get('sid'))
    log_activity('Open remote desktop', node=node.name, status='success')
    ws_path = quote(f'remoteGUI/{seg}/websockify?ticket={ticket}', safe='')
    return redirect(f'/remoteGUI/{seg}/vnc.html?autoconnect=true&resize=remote&reconnect=false&path={ws_path}')


@nodered_bp.route('/internal/nodered-check')
def nodered_check():
    return _check(isolation.NODERED_PREFIX, lambda n: n.nodered_port or 1880)


@nodered_bp.route('/internal/remotegui-check')
def remotegui_check():
    return _check(isolation.REMOTEGUI_PREFIX,
                  lambda n: n.orgui_port or current_app.config.get('ORGUI_DEFAULT_PORT', 6080),
                  ticket_for_websocket=True)
