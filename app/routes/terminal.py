import json
from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required, current_user
from app.models.node import Node
from app.services.ssh_service import execute_command, execute_in_dir
from app.services.rbac import operator_required as operator_required_page
from app.services.activity_service import log_activity
from app import db
from functools import wraps

terminal_bp = Blueprint('terminal', __name__, url_prefix='/terminal')


def operator_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.can_execute():
            return jsonify({'error': 'Access denied'}), 403
        return f(*args, **kwargs)
    return decorated


@terminal_bp.route('/')
@login_required
@operator_required_page
def index():
    nodes = Node.query.filter_by(is_active=True).all()
    return render_template('terminal/index.html', nodes=nodes)


@terminal_bp.route('/execute', methods=['POST'])
@login_required
@operator_required
def execute():
    data = request.get_json()
    command = data.get('command', '').strip()
    node_ids = data.get('node_ids', [])

    if not command:
        return jsonify({'error': 'No command provided'}), 400
    if not node_ids:
        return jsonify({'error': 'No nodes selected'}), 400

    # Block only the most catastrophic commands
    hard_blocked = [':(){ :|:& };:', 'dd if=/dev/zero of=/dev/sda', 'mkfs.ext4 /dev/sd']
    for b in hard_blocked:
        if b in command:
            return jsonify({'error': f'Command blocked: {b}'}), 400

    nodes = Node.query.filter(Node.id.in_(node_ids), Node.is_active == True).all()
    if not nodes:
        return jsonify({'error': 'No valid nodes found'}), 400

    # Per-user, per-node working directory so `cd` persists across commands.
    cwd_map = session.get('term_cwd', {})
    results = {}
    for node in nodes:
        cwd = cwd_map.get(str(node.id))
        result = execute_in_dir(node, command, cwd=cwd)
        cwd_map[str(node.id)] = result.get('cwd') or cwd
        results[node.name] = result
        log_activity(
            action='Terminal Command', node=node.name, command=command,
            response=result.get('stdout', '')[:1000],
            status='success' if result['success'] else 'failure',
            duration=result.get('duration'))
    session['term_cwd'] = cwd_map
    session.modified = True

    return jsonify({'results': results})


@terminal_bp.route('/reset-cwd', methods=['POST'])
@login_required
@operator_required
def reset_cwd():
    session.pop('term_cwd', None)
    session.modified = True
    return jsonify({'success': True})


@terminal_bp.route('/restart', methods=['POST'])
@login_required
@operator_required
def restart_nodes():
    data = request.get_json()
    node_ids = data.get('node_ids', [])

    nodes = Node.query.filter(Node.id.in_(node_ids), Node.is_active == True).all()
    results = {}
    for node in nodes:
        result = execute_command(node, 'sudo reboot', timeout=10)
        results[node.name] = {'triggered': True, 'message': 'Restart command sent'}
        log_activity('Node Restart', node=node.name, command='sudo reboot',
                     status='success', duration=result.get('duration'))

    return jsonify({'results': results})
