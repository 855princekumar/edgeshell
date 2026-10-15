import os, tempfile
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
    query = ActivityLog.query
    if status_filter: query = query.filter_by(status=status_filter)
    if node_filter:   query = query.filter_by(node=node_filter)
    logs  = query.order_by(desc(ActivityLog.timestamp)).paginate(page=page, per_page=50, error_out=False)
    nodes = Node.query.all()
    return render_template('logs/activity.html', logs=logs, nodes=nodes,
                           status_filter=status_filter, node_filter=node_filter)


@logs_bp.route('/browse', methods=['POST'])
@login_required
def browse():
    """List files/dirs on a node path via SSH."""
    data     = request.get_json() or {}
    node_id  = data.get('node_id')
    log_path = data.get('log_path', '/var/log/')
    node     = Node.query.get_or_404(node_id)
    # Use sudo ls if admin
    cmd = f'sudo ls -lah {log_path} 2>/dev/null || ls -lah {log_path} 2>&1'
    result = execute_command(node, cmd, timeout=10)
    return jsonify({'success': result['success'], 'output': result['stdout'],
                    'error': result['stderr'], 'node': node.name})


@logs_bp.route('/fetch', methods=['POST'])
@login_required
def fetch():
    """Fetch (tail) a log file from a node."""
    data      = request.get_json() or {}
    node_id   = data.get('node_id')
    file_path = data.get('file_path', '')
    lines     = int(data.get('lines', 200))
    node      = Node.query.get_or_404(node_id)
    if not file_path:
        return jsonify({'error': 'No file path'}), 400
    cmd    = f'sudo tail -n {lines} {file_path} 2>/dev/null || tail -n {lines} {file_path} 2>&1'
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
    file_path = data.get('file_path', '')
    node      = Node.query.get_or_404(node_id)
    if not file_path:
        return jsonify({'error': 'No file path'}), 400

    try:
        client = get_ssh_client(node, timeout=10)
        sftp   = client.open_sftp()
        filename = os.path.basename(file_path)
        # Write to a temp file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='_' + filename)
        try:
            sftp.get(file_path, tmp.name)
        except Exception:
            # If SFTP get fails (permission), try cat via SSH + write
            _, stdout, _ = client.exec_command(f'sudo cat {file_path} 2>/dev/null', timeout=30)
            tmp.write(stdout.read())
        tmp.close()
        sftp.close()
        client.close()
        log_activity('Log Download', node=node.name, command=file_path, status='success')
        return send_file(tmp.name, as_attachment=True, download_name=filename)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@logs_bp.route('/mkdir', methods=['POST'])
@login_required
@operator_required
def mkdir():
    """Create directory on node with sudo."""
    data     = request.get_json() or {}
    node_id  = data.get('node_id')
    dir_path = data.get('dir_path', '').strip()
    node     = Node.query.get_or_404(node_id)
    if not dir_path:
        return jsonify({'error': 'No directory path'}), 400
    cmd    = f'sudo mkdir -p {dir_path} && sudo chmod 755 {dir_path} && echo "Created: {dir_path}"'
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
