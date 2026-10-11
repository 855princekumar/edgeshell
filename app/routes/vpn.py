"""Backend-agnostic VPN status routes (Tailscale today, WireGuard later)."""
from datetime import datetime
import json
from functools import wraps
from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from app import db, csrf, limiter
from app.models import VpnStatus
from app.services.vpn_service import get_status

vpn_bp = Blueprint('vpn', __name__, url_prefix='/vpn')


@vpn_bp.route('/status')
@login_required
def status():
    return jsonify(get_status())


def _local_or_login(f):
    """Allow the systemd heartbeat timer (direct loopback call, no proxy
    header) or a signed-in user. Anything arriving through Nginx carries
    X-Forwarded-For and must be authenticated."""
    @wraps(f)
    def wrapper(*a, **kw):
        direct_local = (request.remote_addr in ('127.0.0.1', '::1')
                        and 'X-Forwarded-For' not in request.headers)
        if direct_local or current_user.is_authenticated:
            return f(*a, **kw)
        return jsonify({'error': 'unauthorized'}), 401
    return wrapper


@vpn_bp.route('/heartbeat', methods=['POST'])
@csrf.exempt
@limiter.exempt
@_local_or_login
def heartbeat():
    s = get_status()
    row = VpnStatus(connected=s.get('connected', False),
                    ip_address=s.get('self_ip', ''),
                    node_count=s.get('node_count', 0),
                    nodes_json=json.dumps(s.get('nodes', [])),
                    backend_state=s.get('backend_state', 'unknown'),
                    timestamp=datetime.utcnow())
    db.session.add(row)
    db.session.commit()
    return jsonify(s)
