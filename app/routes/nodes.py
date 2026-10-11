import json
from datetime import datetime
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, current_app)
from flask_login import login_required, current_user
from app import db
from app.models.node import Node
from app.models import Deployment
from app.services.ssh_service import test_connection, collect_metrics
from app.services.activity_service import log_activity
from app.services import orgui_service, ansible_service
from app.services.rbac import operator_required, admin_required

nodes_bp = Blueprint('nodes', __name__, url_prefix='/nodes')


def _apply_form(node, form):
    node.name = form.get('name', node.name).strip()
    node.ip_address = form.get('ip_address', node.ip_address).strip()
    node.ssh_port = int(form.get('ssh_port', node.ssh_port or 22) or 22)
    node.username = form.get('username', node.username).strip()
    node.description = form.get('description', node.description)
    node.tags = form.get('tags', node.tags)
    node.environment = form.get('environment', node.environment)
    node.location = form.get('location', node.location)
    node.node_type = form.get('node_type', node.node_type)
    node.nodered_port = int(form.get('nodered_port', node.nodered_port or 1880) or 1880)
    node.orgui_port = int(form.get('orgui_port', node.orgui_port or 6080) or 6080)


@nodes_bp.route('/')
@login_required
def index():
    nodes = Node.query.filter(Node.is_deleted.isnot(True)).all()
    deleted = Node.query.filter(Node.is_deleted.is_(True)) \
        .order_by(Node.deleted_at.desc()).all()
    return render_template('nodes/index.html', nodes=nodes, deleted=deleted)


@nodes_bp.route('/add', methods=['GET', 'POST'])
@login_required
@operator_required
def add():
    if request.method == 'POST':
        quick = request.form.get('quick_mode') == 'on'
        name = request.form.get('name', '').strip()
        ip = request.form.get('ip_address', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # Always-required SSH details
        missing = []
        if not ip:
            missing.append('IP address')
        if not username:
            missing.append('SSH username')
        if not password:
            missing.append('SSH password')
        # Full mode additionally requires identification details
        if not quick:
            if not name:
                missing.append('node name')
            if not request.form.get('environment', '').strip():
                missing.append('environment')
            if not request.form.get('node_type', '').strip():
                missing.append('node type')
            if not request.form.get('location', '').strip():
                missing.append('location')
        if missing:
            flash('Please fill: ' + ', '.join(missing) + '.', 'danger')
            return render_template('nodes/add.html')

        if not name:  # quick mode: derive a name from the IP
            name = ip.replace('.', '-')
        if Node.query.filter_by(name=name).first():
            flash(f'Node "{name}" already exists.', 'danger')
            return render_template('nodes/add.html')

        node = Node(name=name, ip_address='', username='', status='unknown')
        _apply_form(node, request.form)
        node.name = name
        node.ip_address = ip
        node.username = username
        node.password = password
        db.session.add(node)
        db.session.commit()
        log_activity('Add Node', node=name, status='success')
        flash(f'Node "{name}" added successfully.', 'success')
        return redirect(url_for('nodes.index'))
    return render_template('nodes/add.html')


@nodes_bp.route('/<int:node_id>/edit', methods=['GET', 'POST'])
@login_required
@operator_required
def edit(node_id):
    node = Node.query.get_or_404(node_id)
    if request.method == 'POST':
        _apply_form(node, request.form)
        pw = request.form.get('password', '')
        if pw:
            node.password = pw
        node.updated_at = datetime.utcnow()
        db.session.commit()
        log_activity('Edit Node', node=node.name, status='success')
        flash(f'Node "{node.name}" updated.', 'success')
        return redirect(url_for('nodes.index'))
    return render_template('nodes/edit.html', node=node)


@nodes_bp.route('/<int:node_id>/delete', methods=['POST'])
@login_required
@operator_required
def delete(node_id):
    node = Node.query.get_or_404(node_id)
    node.is_deleted = True
    node.deleted_at = datetime.utcnow()
    node.is_active = False  # drops it from monitoring/terminal/deploy lists
    db.session.commit()
    log_activity('Delete Node (recycle bin)', node=node.name, status='success')
    flash(f'Node "{node.name}" moved to the recycle bin.', 'info')
    return redirect(url_for('nodes.index'))


@nodes_bp.route('/<int:node_id>/restore', methods=['POST'])
@login_required
@operator_required
def restore(node_id):
    node = Node.query.get_or_404(node_id)
    node.is_deleted = False
    node.deleted_at = None
    node.is_active = True
    db.session.commit()
    log_activity('Revive Node', node=node.name, status='success')
    flash(f'Node "{node.name}" restored to the fleet.', 'success')
    return redirect(url_for('nodes.index'))


@nodes_bp.route('/<int:node_id>/purge', methods=['POST'])
@login_required
@admin_required
def purge(node_id):
    node = Node.query.get_or_404(node_id)
    name = node.name
    db.session.delete(node)
    db.session.commit()
    log_activity('Purge Node (permanent)', node=name, status='success')
    flash(f'Node "{name}" permanently deleted.', 'warning')
    return redirect(url_for('nodes.index'))


@nodes_bp.route('/<int:node_id>/toggle', methods=['POST'])
@login_required
@operator_required
def toggle(node_id):
    node = Node.query.get_or_404(node_id)
    node.is_active = not node.is_active
    db.session.commit()
    state = 'enabled' if node.is_active else 'disabled'
    log_activity(f'Node {state}', node=node.name, status='success')
    return jsonify({'success': True, 'active': node.is_active})


@nodes_bp.route('/<int:node_id>/test', methods=['POST'])
@login_required
def test(node_id):
    node = Node.query.get_or_404(node_id)
    success, message = test_connection(node)
    node.status = 'online' if success else 'offline'
    node.last_seen = datetime.utcnow() if success else node.last_seen
    if success:
        # capture architecture so the fleet can categorize 32 vs 64 bit
        from app.services.ssh_service import execute_command
        r = execute_command(node, 'uname -m', timeout=10)
        lines = [l.strip() for l in (r.get('stdout') or '').splitlines() if l.strip()]
        if lines:
            node.arch = lines[-1]
    db.session.commit()
    log_activity('Test Connection', node=node.name, response=message,
                 status='success' if success else 'failure')
    return jsonify({'success': success, 'message': message,
                    'arch': node.arch, 'arch_bits': node.arch_bits})


@nodes_bp.route('/<int:node_id>/metrics', methods=['POST'])
@login_required
def metrics(node_id):
    node = Node.query.get_or_404(node_id)
    data = collect_metrics(node)
    if data:
        node.cpu_percent = data.get('cpu_percent')
        node.ram_percent = data.get('mem_percent')       # fixed key
        node.disk_percent = data.get('disk_percent')
        node.load_average = f"{data.get('load_1',0)} {data.get('load_5',0)} {data.get('load_15',0)}"
        node.arch = data.get('arch', node.arch)
        node.status = 'online'
        node.last_seen = datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True, 'metrics': data})
    return jsonify({'success': False, 'message': 'Could not collect metrics'})


# ── OpenRemoteGUI ────────────────────────────────────────────────────────────
@nodes_bp.route('/<int:node_id>/orgui/status', methods=['POST'])
@login_required
@operator_required
def orgui_status(node_id):
    node = Node.query.get_or_404(node_id)
    installed, signal = orgui_service.persist_check(node)
    url = f"http://{node.ip_address}:{node.orgui_port or current_app.config['ORGUI_DEFAULT_PORT']}"
    return jsonify({'success': True, 'installed': installed,
                    'signal': signal, 'url': url})


@nodes_bp.route('/<int:node_id>/orgui/install', methods=['POST'])
@login_required
@operator_required
def orgui_install(node_id):
    node = Node.query.get_or_404(node_id)
    ok, output = orgui_service.install(node)
    log_activity('OpenRemoteGUI install', node=node.name,
                 response=output[-500:], status='success' if ok else 'failure')
    return jsonify({'success': ok, 'output': output[-4000:]})


@nodes_bp.route('/orgui/scan', methods=['POST'])
@login_required
@operator_required
def orgui_scan():
    nodes = Node.query.filter_by(is_active=True).all()
    summary = orgui_service.scan_fleet(nodes)
    log_activity('OpenRemoteGUI fleet scan',
                 response=f"{summary['installed']}/{summary['total']} installed",
                 status='success')
    return jsonify(summary)


@nodes_bp.route('/orgui/install-all', methods=['POST'])
@login_required
@operator_required
def orgui_install_all():
    nodes = Node.query.filter_by(is_active=True).all()
    missing = [n for n in nodes if n.orgui_installed is not True]
    targets = missing or nodes
    playbook = current_app.config['ANSIBLE_PLAYBOOK_DIR'] + '/orgui_install.yml'
    results = ansible_service.run_playbook(targets, playbook)

    ok = sum(1 for r in results.values() if r['success'])
    fail = len(results) - ok
    dep = Deployment(user=current_user.username, filename='orgui_install.yml',
                     target_path='fleet', nodes=json.dumps([n.name for n in targets]),
                     status='success' if fail == 0 else 'partial',
                     success_count=ok, failure_count=fail,
                     result=json.dumps(results))
    db.session.add(dep)
    for n in targets:
        r = results.get(n.name, {})
        if r.get('success'):
            n.orgui_installed = True
            n.orgui_checked_at = datetime.utcnow()
    db.session.commit()
    log_activity('OpenRemoteGUI fleet install',
                 response=f'{ok} ok, {fail} failed', status='success' if fail == 0 else 'warning')
    return jsonify({'success': fail == 0, 'ok': ok, 'failed': fail,
                    'results': results})


# ── Bulk import (CSV / TXT) ──────────────────────────────────────────────────
import io as _io
from flask import session as _session
from app.services import import_service as _imp
from app.services.ssh_service import test_connection as _testconn


@nodes_bp.route('/import', methods=['GET', 'POST'])
@login_required
@operator_required
def bulk_import():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f:
            flash('No file uploaded.', 'danger')
            return render_template('nodes/import.html')
        text = f.read().decode('utf-8', errors='replace')
        mapping, rows, headers = _imp.parse(text)
        if not rows:
            flash('Could not parse any rows from the file.', 'danger')
            return render_template('nodes/import.html')
        _session['import_rows'] = rows
        _session['import_headers'] = headers
        return render_template('nodes/import_verify.html', mapping=mapping,
                               rows=rows[:200], headers=headers,
                               fields=list(_imp.FIELD_ALIASES.keys()),
                               total=len(rows))
    return render_template('nodes/import.html')


@nodes_bp.route('/import/commit', methods=['POST'])
@login_required
@operator_required
def bulk_import_commit():
    rows = _session.get('import_rows', [])
    headers = _session.get('import_headers', [])
    mapping = {h: (request.form.get(f'map__{h}') or None) for h in headers}
    for k, v in list(mapping.items()):
        if v in ('', 'None'):
            mapping[k] = None
    nodes_data, errors = _imp.to_nodes(
        rows, mapping,
        default_password=request.form.get('default_password', ''),
        host_override=request.form.get('host_override', '').strip())

    created, skipped = 0, 0
    for rec in nodes_data:
        if Node.query.filter_by(name=rec['name']).first():
            skipped += 1
            continue
        pw = rec.pop('password', '')
        node = Node(status='unknown', **{k: v for k, v in rec.items()
                    if k in Node.__table__.columns.keys()})
        if pw:
            node.password = pw
        db.session.add(node)
        created += 1
    db.session.commit()
    _session.pop('import_rows', None)
    _session.pop('import_headers', None)
    log_activity('Bulk import', response=f'{created} created, {skipped} skipped',
                 status='success')

    # Immediate reachability + ORGUI pass over the freshly created fleet.
    fresh = Node.query.filter_by(is_active=True).all()
    reachable = 0
    for n in fresh:
        ok, _ = _testconn(n)
        n.status = 'online' if ok else 'offline'
        if ok:
            reachable += 1
            orgui_service.persist_check(n)
    db.session.commit()
    orgui_count = sum(1 for n in fresh if n.orgui_installed is True)

    flash(f'Imported {created} node(s), skipped {skipped}. '
          f'{reachable}/{len(fresh)} reachable, {orgui_count} have OpenRemoteGUI.',
          'success')
    return redirect(url_for('nodes.index'))


# ── Fleet capabilities (Docker / Node-RED / OpenRemoteGUI) ───────────────────
from app.services import capability_service as _cap


@nodes_bp.route('/capabilities/scan', methods=['POST'])
@login_required
@operator_required
def capabilities_scan():
    nodes = Node.query.filter_by(is_active=True).all()
    summary = _cap.scan_fleet(nodes)
    log_activity('Capability scan',
                 response=', '.join(f"{c}:{summary['counts'][c]['installed']}/{summary['total']}"
                                    for c in _cap.CAPABILITIES),
                 status='success')
    return jsonify(summary)


@nodes_bp.route('/capabilities/install', methods=['POST'])
@login_required
@operator_required
def capabilities_install():
    data = request.get_json() or {}
    capability = data.get('capability')
    if capability not in _cap.CAPABILITIES:
        return jsonify({'error': 'invalid capability'}), 400
    node_ids = data.get('node_ids')
    q = Node.query.filter_by(is_active=True)
    if node_ids:
        q = q.filter(Node.id.in_(node_ids))
    else:
        # default: nodes missing this capability
        col = getattr(Node, f'{capability}_installed')
        q = q.filter(col.isnot(True))
    targets = q.all()

    results, ok = {}, 0
    for n in targets:
        success, output = _cap.install(n, capability)
        results[n.name] = {'success': success, 'output': output[-800:]}
        ok += 1 if success else 0
    fail = len(targets) - ok
    log_activity(f'Install {capability} on fleet',
                 response=f'{ok} ok, {fail} failed', status='success' if fail == 0 else 'warning')
    return jsonify({'success': fail == 0, 'capability': capability,
                    'ok': ok, 'failed': fail, 'results': results})
