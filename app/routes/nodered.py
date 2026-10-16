"""Node-RED of every node at /nodered/<node>/ on EdgeShell's own address (ADR 0014).

nginx proxies /nodered/<node>/... to that node's Node-RED after asking
/internal/nodered-check, which allows signed-in operators and administrators
and returns the node's address in a header nginx keeps to itself. The browser
never sees the node's IP, never needs the VPN, and Node-RED never sees
EdgeShell's cookies.
"""
import re

from flask import Blueprint, redirect, request
from flask_login import current_user, login_required

from app.models.node import Node
from app.services import isolation
from app.services.activity_service import log_activity

nodered_bp = Blueprint('nodered', __name__)

SAFE = re.compile(r'[A-Za-z0-9._-]{1,100}')
HOST = re.compile(r'[A-Za-z0-9.-]{1,253}')            # IPv4 or a DNS name; nothing nginx could read as a path


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


@nodered_bp.route('/nodes/<int:node_id>/nodered')
@login_required
def open_nodered(node_id):
    """The Node-RED button: logs the opening and goes to /nodered/<node>/."""
    if not current_user.can_execute():
        return 'Opening Node-RED needs the operator role.', 403
    node = Node.query.filter(Node.id == node_id, Node.is_deleted.isnot(True)).first_or_404()
    log_activity('Open Node-RED', node=node.name, status='success')
    return redirect(f'/nodered/{segment_for(node)}/')


@nodered_bp.route('/internal/nodered-check')
def nodered_check():
    """nginx auth_request for /nodered/<node>/...: 204 with X-NodeRed-Upstream
    (ip:port) for a signed-in operator or administrator, 401 when signed out, 403
    otherwise. Not reachable from outside: nginx blocks /internal/."""
    uri = request.headers.get('X-Original-URI', '')
    m = re.match(r'^/nodered/([^/?#]+)', uri)
    if not m:
        return '', 403
    if not current_user.is_authenticated:
        return '', 401
    if not current_user.can_execute() or isolation.nodered_reason(request, m.group(1)):
        return '', 403
    node = _node_from(m.group(1))
    if node is None or not HOST.fullmatch(node.ip_address or ''):
        return '', 403                                   # only a plain host or IP ever reaches nginx
    port = node.nodered_port or 1880
    if not 1 <= int(port) <= 65535:
        return '', 403
    resp = ('', 204, {'X-NodeRed-Upstream': f'{node.ip_address}:{int(port)}'})
    return resp
