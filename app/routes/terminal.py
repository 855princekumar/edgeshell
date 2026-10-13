import json
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, Response, render_template, request, jsonify, stream_with_context
from flask_login import login_required, current_user
from app.models.node import Node
from app.services.ssh_service import execute_command
from app.services.rbac import operator_required as operator_required_page
from app.services.activity_service import log_activity
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


# Working directory per (user, node), so `cd` persists across commands. Kept
# on the server: results stream, so the session cookie cannot change mid-reply.
_CWD = {}
_CWD_MARKER = '__EDGESHELL_CWD__'
PARALLEL = 16


def _wrapped(command, cwd):
    prefix = f'cd {shlex.quote(cwd)} 2>/dev/null; ' if cwd else ''
    return (f'{prefix}{command}\n__ec=$?\n'
            f'printf "%s%s\\n" "{_CWD_MARKER}" "$(pwd)"\nexit $__ec')


def _split_cwd(stdout, cwd):
    kept, new_cwd = [], cwd
    for line in stdout.splitlines():
        if line.rstrip('\r').startswith(_CWD_MARKER):
            new_cwd = line.rstrip('\r')[len(_CWD_MARKER):].strip() or new_cwd
        else:
            kept.append(line)
    return '\n'.join(kept), new_cwd


@terminal_bp.route('/execute', methods=['POST'])
@login_required
@operator_required
def execute():
    """Run a command on the selected nodes in parallel and stream one JSON
    line per node as each finishes (application/x-ndjson).

    Nodes that are not online (offline, login failed) are skipped up front
    with the reason, so they never hold up the others. sudo uses the node's
    stored password, sent safely (see collector.run_command)."""
    from app.services import collector
    data = request.get_json() or {}
    command = (data.get('command') or '').strip()
    node_ids = data.get('node_ids') or []
    timeout = max(5, min(int(data.get('timeout') or 120), 1800))
    if not command:
        return jsonify({'error': 'No command provided'}), 400
    if not node_ids:
        return jsonify({'error': 'No nodes selected'}), 400
    hard_blocked = [':(){ :|:& };:', 'dd if=/dev/zero of=/dev/sda', 'mkfs.ext4 /dev/sd']
    for blocked in hard_blocked:
        if blocked in command:
            return jsonify({'error': f'Command blocked: {blocked}'}), 400

    nodes = Node.query.filter(Node.id.in_(node_ids), Node.is_active.is_(True)).all()
    if not nodes:
        return jsonify({'error': 'No valid nodes found'}), 400
    reasons = {'auth_failed': 'login failed', 'offline': 'offline', 'unknown': 'not checked yet'}
    run_on = [n for n in nodes if n.status == 'online']
    skipped = [{'id': n.id, 'name': n.name, 'reason': reasons.get(n.status, n.status or 'unknown')}
               for n in nodes if n.status != 'online']
    targets = [collector.target_for(n) for n in run_on]
    uid = current_user.id

    def one(t):
        cwd = _CWD.get((uid, t['id']))
        r = collector.run_command(t, _wrapped(command, cwd), timeout=timeout,
                                  sudo_password=t['password'])
        out, new_cwd = _split_cwd(r['stdout'], cwd)
        _CWD[(uid, t['id'])] = new_cwd
        return t, dict(r, stdout=out, cwd=new_cwd)

    def stream():
        yield json.dumps({'type': 'start', 'running': [{'id': t['id'], 'name': t['name']} for t in targets],
                          'skipped': skipped}) + '\n'
        if targets:
            with ThreadPoolExecutor(max_workers=min(PARALLEL, len(targets))) as pool:
                futures = [pool.submit(one, t) for t in targets]
                for f in as_completed(futures):
                    t, r = f.result()
                    log_activity(action='Terminal Command', node=t['name'], command=command,
                                 response=(r['stdout'] or r['error'] or '')[:1000],
                                 status='success' if r['success'] else 'failure',
                                 duration=r['duration'])
                    yield json.dumps({'type': 'result', 'id': t['id'], 'node': t['name'],
                                      'stdout': r['stdout'], 'stderr': r['error'] or '',
                                      'exit_code': r['exit_code'], 'success': r['success'],
                                      'duration': r['duration'], 'cwd': r['cwd']}) + '\n'
        yield json.dumps({'type': 'done'}) + '\n'

    return Response(stream_with_context(stream()), mimetype='application/x-ndjson',
                    headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})


@terminal_bp.route('/reset-cwd', methods=['POST'])
@login_required
@operator_required
def reset_cwd():
    for key in [k for k in _CWD if k[0] == current_user.id]:
        _CWD.pop(key, None)
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
