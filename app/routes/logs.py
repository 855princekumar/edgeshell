import os, shlex, tempfile
from flask import Blueprint, render_template, request, jsonify, send_file, abort
from flask_login import login_required, current_user
from app import db
from app.models.node import Node
from app.models import NodeLog, ActivityLog
from app.services.ssh_service import execute_command, get_ssh_client
from app.services.log_service import format_log, get_log_summary, detect_severity
from app.services.activity_service import log_activity
from datetime import datetime
from sqlalchemy import desc
from functools import wraps
from app.services.rbac import admin_required

logs_bp = Blueprint('logs', __name__, url_prefix='/logs')


def _node_path(value, default=None):
    """A path on a node, safe to put in a shell command: absolute, at most
    1024 characters, no control characters, and shell-quoted. Every caller
    runs it through this, so a crafted path can never add a command (these
    routes run `sudo` on the node and are open to every signed-in role).
    Returns (quoted, plain) or (None, None)."""
    value = default if value in (None, '') else value
    if not isinstance(value, str) or not value.startswith('/') or len(value) > 1024 \
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None, None
    return shlex.quote(value), value


def operator_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.can_execute():
            return jsonify({'error': 'Access denied'}), 403
        return f(*args, **kwargs)
    return decorated


@logs_bp.route('/')
@login_required
def index():
    nodes = Node.query.filter_by(is_active=True).all()
    return render_template('logs/index.html', nodes=nodes)


@logs_bp.route('/activity')
@login_required
@admin_required
def activity():
    """The audit trail: administrators only (operators and monitors neither
    see it nor reach it, ADR 0004)."""
    page          = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', '')
    node_filter   = request.args.get('node', '')
    user_filter   = request.args.get('user', '')[:64]
    query = ActivityLog.query
    if status_filter: query = query.filter_by(status=status_filter)
    if node_filter:   query = query.filter_by(node=node_filter)
    if user_filter:   query = query.filter_by(user=user_filter)
    logs  = query.order_by(desc(ActivityLog.timestamp)).paginate(page=page, per_page=50, error_out=False)
    nodes = Node.query.all()
    # Everyone who appears in the log, including deleted accounts and 'system'
    # (the log keeps the name used at the time). Served by the user index.
    users = [u for (u,) in db.session.query(ActivityLog.user).filter(ActivityLog.user.isnot(None))
             .distinct().order_by(ActivityLog.user).limit(1000)]
    return render_template('logs/activity.html', logs=logs, nodes=nodes, users=users,
                           status_filter=status_filter, node_filter=node_filter, user_filter=user_filter)


@logs_bp.route('/browse', methods=['POST'])
@login_required
def browse():
    """List files/dirs on a node path via SSH."""
    data     = request.get_json() or {}
    node_id  = data.get('node_id')
    node     = Node.query.get_or_404(node_id)
    path, _ = _node_path(data.get('log_path'), '/var/log/')
    if not path:
        return jsonify({'success': False, 'error': 'Enter an absolute path such as /var/log/'}), 400
    cmd = f'sudo -n ls -lah -- {path} 2>/dev/null || ls -lah -- {path} 2>&1'
    result = execute_command(node, cmd, timeout=10)
    return jsonify({'success': result['success'], 'output': result['stdout'],
                    'error': result['stderr'], 'node': node.name})


@logs_bp.route('/fetch', methods=['POST'])
@login_required
def fetch():
    """Fetch (tail) a log file from a node."""
    data      = request.get_json() or {}
    node_id   = data.get('node_id')
    try:
        lines = max(1, min(int(data.get('lines', 200)), 10000))
    except (TypeError, ValueError):
        lines = 200
    node      = Node.query.get_or_404(node_id)
    path, file_path = _node_path(data.get('file_path'))
    if not path:
        return jsonify({'error': 'Enter an absolute file path'}), 400
    cmd    = f'sudo -n tail -n {lines} -- {path} 2>/dev/null || tail -n {lines} -- {path} 2>&1'
    result = execute_command(node, cmd, timeout=15)
    log_activity('Log Fetch', node=node.name, command=file_path, status='success' if result['success'] else 'failure')
    return jsonify({'success': result['success'], 'content': result['stdout'],
                    'stderr': result['stderr'], 'node': node.name, 'path': file_path})


@logs_bp.route('/download', methods=['POST'])
@login_required
@operator_required
def download():
    """Download a log file from a node via SFTP to local, then serve it."""
    data      = request.get_json() or {}
    node_id   = data.get('node_id')
    node      = Node.query.get_or_404(node_id)
    path, file_path = _node_path(data.get('file_path'))
    if not path:
        return jsonify({'error': 'Enter an absolute file path'}), 400

    from werkzeug.utils import secure_filename
    try:
        client = get_ssh_client(node, timeout=10)
        sftp   = client.open_sftp()
        filename = secure_filename(os.path.basename(file_path)) or 'log.txt'
        # A local temp file with a fixed name pattern (nothing from the request in its path)
        tmp = tempfile.NamedTemporaryFile(delete=False, prefix='edgeshell-log-', suffix='.txt')
        try:
            sftp.get(file_path, tmp.name)
        except Exception:
            # If SFTP get fails (permission), try cat via SSH + write
            # Bandit B601 reviewed: the path is validated and shell-quoted by _node_path
            _, stdout, _ = client.exec_command(f'sudo -n cat -- {path} 2>/dev/null', timeout=30)  # nosec B601
            tmp.write(stdout.read())
        tmp.close()
        sftp.close()
        client.close()
        log_activity('Log Download', node=node.name, command=file_path, status='success')
        return send_file(tmp.name, as_attachment=True, download_name=filename)
    except Exception as e:
        from app.services.errors import describe
        return jsonify({'error': describe(e, 'log download')}), 500


@logs_bp.route('/mkdir', methods=['POST'])
@login_required
@operator_required
def mkdir():
    """Create directory on node with sudo."""
    data     = request.get_json() or {}
    node_id  = data.get('node_id')
    node     = Node.query.get_or_404(node_id)
    path, dir_path = _node_path((data.get('dir_path') or '').strip())
    if not path:
        return jsonify({'error': 'Enter an absolute directory path'}), 400
    cmd    = f'sudo -n mkdir -p -- {path} && sudo -n chmod 755 -- {path} && echo Created'
    result = execute_command(node, cmd, timeout=10)
    log_activity('Create Directory', node=node.name, command=dir_path,
                 status='success' if result['success'] else 'failure')
    return jsonify({'success': result['success'], 'output': result['stdout'],
                    'error': result['stderr']})


@logs_bp.route('/node/<node_name>')
@login_required
def node_logs(node_name):
    node = Node.query.filter_by(name=node_name).first_or_404()
    logs = NodeLog.query.filter_by(node_name=node_name).order_by(
        desc(NodeLog.collection_time)).limit(200).all()
    return render_template('logs/node_logs.html', node=node, logs=logs)
