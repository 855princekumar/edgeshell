import json
from datetime import datetime
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, current_app)
from flask_login import login_required, current_user
from app import db
from app.models.node import Node
from app.services.activity_service import log_activity
from app.services import orgui_service
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
    from app.services import categories as cats
    categories = cats.load()
    for n in nodes:
        n.category = cats.category_of(n.node_type, categories)
    return render_template('nodes/index.html', nodes=nodes, deleted=deleted,
                           categories=categories,
                           has_other=any(n.category == 'Other' for n in nodes))


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
        from app.services.collector import COLLECTOR
        COLLECTOR.kick([node.id])
        log_activity('Add Node', node=name, status='success')
        flash(f'Node "{name}" added. Its SSH login is being checked in the background; '
              'the card updates in a few seconds.', 'success')
        return redirect(url_for('nodes.index'))
    return render_template('nodes/add.html')


@nodes_bp.route('/<int:node_id>/edit', methods=['GET', 'POST'])
@login_required
@operator_required
def edit(node_id):
    node = Node.query.get_or_404(node_id)
    if request.method == 'POST':
        before = (node.ip_address, node.ssh_port, node.username)
        _apply_form(node, request.form)
        pw = request.form.get('password', '')
        if pw:
            node.password = pw
        changed = bool(pw) or before != (node.ip_address, node.ssh_port, node.username)
        if changed and node.login_paused_at is not None:
            from app.services import login_policy
            login_policy.resume(node)                 # new login details: try them
        node.updated_at = datetime.utcnow()
        db.session.commit()
        if changed:
            from app.services.collector import COLLECTOR, POOL
            POOL.drop(node.id)
            COLLECTOR.kick([node.id])
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


def _purge_nodes(nodes):
    """Delete nodes for good from the hot database, with their history rows.
    The data lake keeps the node and its history: the name is first changed
    (on both databases) to `name~deleted-<id>` so a new node may reuse the
    name, then the rows are deleted on the hot database only."""
    from app.services import lake
    from app.services.collector import POOL
    from sqlalchemy.exc import IntegrityError
    ids = [n.id for n in nodes]
    if not ids:
        return 0
    lake.require_ready()                        # refuse before changing anything
    for n in nodes:
        POOL.drop(n.id)
        n.name = lake.tombstone(n.name, n.id)
    db.session.commit()
    marks = ','.join(f':i{k}' for k in range(len(ids)))
    params = {f'i{k}': v for k, v in enumerate(ids)}
    # Bandit B608 reviewed: only placeholder names (:i0, :i1, ...) are formatted in; values are bound
    try:
        lake.hot_only([(f'DELETE FROM system_metrics WHERE node_id IN ({marks})', params),  # nosec B608
                       # Bandit B608 reviewed: only placeholder names (:i0, :i1, ...) are formatted in; values are bound
                       (f'DELETE FROM metrics_hourly WHERE node_id IN ({marks})', params),  # nosec B608
                       # Bandit B608 reviewed: only placeholder names (:i0, :i1, ...) are formatted in; values are bound
                       (f'DELETE FROM nodes WHERE id IN ({marks})', params)])  # nosec B608
    except IntegrityError:
        # A poll already under way wrote a sample in between; nothing was
        # deleted (one transaction) and a retry finishes the job.
        db.session.rollback()
        raise lake.LakeNotReady('a node was still being polled; try again in a minute')
    return len(ids)


def _ids(data, limit=5000):
    try:
        ids = [int(i) for i in (data.get('node_ids') or [])][:limit]
    except (TypeError, ValueError):
        ids = []
    return ids


# ── Bulk actions on selected nodes ───────────────────────────────────────────
@nodes_bp.route('/bulk/delete', methods=['POST'])
@login_required
@operator_required
def bulk_delete():
    """Move the selected nodes to the recycle bin (undo: Restore)."""
    from app.services.collector import POOL
    ids = _ids(request.get_json(silent=True) or {})
    nodes = Node.query.filter(Node.id.in_(ids), Node.is_deleted.isnot(True)).all() if ids else []
    if not nodes:
        return jsonify({'success': False, 'message': 'Select at least one node.'}), 400
    now = datetime.utcnow()
    for n in nodes:
        n.is_deleted, n.deleted_at, n.is_active = True, now, False
        POOL.drop(n.id)
    db.session.commit()
    log_activity('Bulk delete (recycle bin)', response=f'{len(nodes)} node(s): '
                 + ', '.join(n.name for n in nodes[:20]) + (' ...' if len(nodes) > 20 else ''),
                 status='success')
    return jsonify({'success': True, 'count': len(nodes),
                    'message': f'Moved {len(nodes)} node(s) to the recycle bin. Restore brings them back.'})


@nodes_bp.route('/bulk/restore', methods=['POST'])
@login_required
@operator_required
def bulk_restore():
    data = request.get_json(silent=True) or {}
    q = Node.query.filter(Node.is_deleted.is_(True))
    if not data.get('all'):
        q = q.filter(Node.id.in_(_ids(data) or [0]))
    nodes = q.all()
    for n in nodes:
        n.is_deleted, n.deleted_at, n.is_active = False, None, True
    db.session.commit()
    if nodes:
        from app.services.collector import COLLECTOR
        COLLECTOR.kick([n.id for n in nodes])
    log_activity('Bulk restore', response=f'{len(nodes)} node(s)', status='success')
    return jsonify({'success': True, 'count': len(nodes), 'message': f'Restored {len(nodes)} node(s).'})


@nodes_bp.route('/bulk/purge', methods=['POST'])
@login_required
@admin_required
def bulk_purge():
    """Delete recycle-bin nodes for good (typed confirmation)."""
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'DELETE':
        return jsonify({'success': False, 'message': 'Type DELETE to confirm.'}), 400
    q = Node.query.filter(Node.is_deleted.is_(True))           # only from the recycle bin
    if not data.get('all'):
        q = q.filter(Node.id.in_(_ids(data) or [0]))
    nodes = q.all()
    names = [n.name for n in nodes]
    from app.services import lake
    from app.services.lake import LakeNotReady
    try:
        count = _purge_nodes(nodes)
    except LakeNotReady as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Not deleted: the data lake must be caught up first ({lake.refusal_detail()}).'}), 409
    log_activity('Bulk purge (permanent)', response=f'{count} node(s): ' + ', '.join(names[:20])
                 + (' ...' if count > 20 else ''), status='warning')
    return jsonify({'success': True, 'count': count, 'message': f'Deleted {count} node(s) permanently.'})


def _check_logins(node_ids):
    """Background job: one login per node with its new details."""
    from app.services.collector import COLLECTOR
    results = COLLECTOR.run_cycle(node_ids=node_ids, force=True, manual=True)
    return sum(1 for r in results.values() if r.get('ok'))


@nodes_bp.route('/bulk/credentials', methods=['POST'])
@login_required
@operator_required
def bulk_credentials():
    """Set the SSH username, password and/or port of the selected nodes, then
    check each once in the background. Empty fields keep the current value."""
    from app.services import login_policy
    from app.services.collector import POOL
    data = request.get_json(silent=True) or {}
    ids = _ids(data)
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    port = str(data.get('ssh_port') or '').strip()
    if not ids:
        return jsonify({'success': False, 'message': 'Select at least one node.'}), 400
    if not (username or password or port):
        return jsonify({'success': False, 'message': 'Enter a username, a password or a port to change.'}), 400
    if port and (not port.isdigit() or not 0 < int(port) < 65536):
        return jsonify({'success': False, 'message': 'The SSH port must be 1 to 65535.'}), 400
    if len(username) > 64 or len(password) > 256:
        return jsonify({'success': False, 'message': 'Username or password is too long.'}), 400
    nodes = Node.query.filter(Node.id.in_(ids), Node.is_deleted.isnot(True)).all()
    for n in nodes:
        if username:
            n.username = username
        if password:
            n.password = password
        if port:
            n.ssh_port = int(port)
        login_policy.resume(n)                         # new details: one attempt each
        n.updated_at = datetime.utcnow()
        POOL.drop(n.id)
    db.session.commit()
    changed = ', '.join(f for f, v in (('username', username), ('password', password), ('port', port)) if v)
    log_activity('Bulk login update', response=f'{changed} on {len(nodes)} node(s)', status='success')
    jid = _jobs.run_batches(
        'login-check', f'Checking the new login on {len(nodes)} node(s)', current_user.id,
        [n.id for n in nodes], _check_logins,
        summary=lambda j: f"{j['ok']} of {j['total']} node(s) log in with the new details"
                          + (f"; {j['failed']} still fail (they pause again)" if j['failed'] else ''))
    return jsonify({'success': True, 'count': len(nodes), 'job_id': jid,
                    'message': f'Updated {changed} on {len(nodes)} node(s). Each is checked once now; '
                               'progress shows at the bottom of the page.'})


@nodes_bp.route('/<int:node_id>/purge', methods=['POST'])
@login_required
@admin_required
def purge(node_id):
    node = Node.query.get_or_404(node_id)
    name = node.name
    from app.services import lake
    from app.services.lake import LakeNotReady
    try:
        _purge_nodes([node])
    except LakeNotReady as e:
        db.session.rollback()
        flash(f'Not deleted: the data lake must be caught up first ({lake.refusal_detail()}).', 'danger')
        return redirect(url_for('nodes.index'))
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
    from app.services.collector import COLLECTOR
    node = Node.query.get_or_404(node_id)
    # Test on one card is a person's single attempt, allowed for a paused node;
    # Test All sends bulk=1 and leaves paused nodes alone.
    bulk = (request.get_json(silent=True) or {}).get('bulk') or request.args.get('bulk')
    if bulk and node.login_paused_at is not None:
        from app.services.ssh_service import PAUSED_MESSAGE
        return jsonify({'success': False, 'message': PAUSED_MESSAGE, 'status': node.status,
                        'kind': 'auth', 'paused': True})
    r = COLLECTOR.run_cycle(node_ids=[node.id], force=True, manual=True).get(node.id)
    node = db.session.get(Node, node_id)
    ok = bool(r and r['ok'])
    message = 'Connection successful' if ok else (r['message'] if r else 'Node is inactive')
    log_activity('Test Connection', node=node.name, response=message,
                 status='success' if ok else 'failure')
    return jsonify({'success': ok, 'message': message, 'status': node.status,
                    'kind': None if ok else (r or {}).get('kind'),
                    'seconds': (r or {}).get('seconds'),
                    'arch': node.arch, 'arch_bits': node.arch_bits})


@nodes_bp.route('/<int:node_id>/metrics', methods=['POST'])
@login_required
def metrics(node_id):
    from app.services.collector import COLLECTOR
    node = Node.query.get_or_404(node_id)
    r = COLLECTOR.run_cycle(node_ids=[node.id], force=True).get(node.id)
    if r is None and node.login_paused_at is not None:
        from app.services.ssh_service import PAUSED_MESSAGE
        return jsonify({'success': False, 'message': PAUSED_MESSAGE, 'kind': 'auth', 'paused': True})
    if r and r['ok']:
        return jsonify({'success': True, 'metrics': r['metrics']})
    return jsonify({'success': False, 'message': (r or {}).get('message') or 'Could not collect metrics',
                    'kind': (r or {}).get('kind')})


@nodes_bp.route('/<int:node_id>/credentials', methods=['POST'])
@login_required
@operator_required
def credentials(node_id):
    """Fix a node's SSH login from its card, then check it right away."""
    from app.services.collector import COLLECTOR, POOL
    node = Node.query.get_or_404(node_id)
    data = request.get_json(silent=True) or request.form
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    port = str(data.get('ssh_port') or node.ssh_port or 22).strip()
    if not username or not port.isdigit() or not 0 < int(port) < 65536:
        return jsonify({'success': False, 'message': 'Enter a username and a valid SSH port.'}), 400
    node.username = username
    node.ssh_port = int(port)
    if password:
        node.password = password
    from app.services import login_policy
    login_policy.resume(node)                         # new login details: one attempt now
    db.session.commit()
    POOL.drop(node.id)
    r = COLLECTOR.run_cycle(node_ids=[node.id], force=True, manual=True).get(node.id) or {}
    node = db.session.get(Node, node_id)
    log_activity('Update SSH credentials', node=node.name,
                 response='login ok' if r.get('ok') else r.get('message'),
                 status='success' if r.get('ok') else 'warning')
    return jsonify({'success': bool(r.get('ok')), 'status': node.status,
                    'kind': r.get('kind'), 'seconds': r.get('seconds'),
                    'message': 'Saved. SSH login works.' if r.get('ok')
                    else f"Saved, but the check failed: {r.get('message')}"})


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
@admin_required            # installs software on nodes: administrators only
def orgui_install(node_id):
    """Guided install on one node as a background job (steps + live log)."""
    node = Node.query.get_or_404(node_id)
    jid = _jobs.run_task('orgui-install', f'Installing OpenRemoteGUI on {node.name}',
                         current_user.id, 1,
                         lambda task: orgui_service.install_task(task, [node.id]),
                         summary=_orgui_summary)
    log_activity('OpenRemoteGUI install started', node=node.name, status='info')
    return jsonify({'success': True, 'job_id': jid})


def _orgui_summary(job):
    parts = [f"{job['ok']} installed"]
    skipped = sum(1 for i in job['items'] if i.get('state') == 'skipped')
    failed = job['failed'] - skipped
    if failed:
        parts.append(f'{failed} failed')
    if skipped:
        parts.append(f'{skipped} skipped (see the reasons)')
    return ', '.join(parts)


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
@admin_required            # installs software on nodes: administrators only
def orgui_install_all():
    """Install on every online node that does not run it yet, one node at a
    time, stopping at the first failure so a problem cannot spread across a
    production fleet. Nodes that fail the safety checks are skipped."""
    nodes = (Node.query.filter(Node.is_active.is_(True), Node.is_deleted.isnot(True),
                               Node.status == 'online')
             .filter(db.or_(Node.orgui_installed.is_(None), Node.orgui_installed.is_(False)))
             .order_by(Node.name).all())
    if not nodes:
        return jsonify({'success': False, 'message': 'Every online node already runs OpenRemoteGUI.'})
    ids = [n.id for n in nodes]
    jid = _jobs.run_task('orgui-install', f'Installing OpenRemoteGUI on {len(ids)} node(s)',
                         current_user.id, len(ids),
                         lambda task: orgui_service.install_task(task, ids, stop_on_failure=True),
                         summary=_orgui_summary)
    log_activity('OpenRemoteGUI fleet install started', response=f'{len(ids)} node(s)', status='info')
    return jsonify({'success': True, 'job_id': jid, 'count': len(ids)})


# ── Bulk import (CSV / TXT / JSON) ───────────────────────────────────────────
# The parsed rows wait on the server between upload and commit (only a short
# token sits in the session cookie: whole files in the cookie overflowed the
# proxy's header limit and browsers' 4 KB cookie limit). After the commit the
# new nodes are checked over SSH in a background job with live progress.
import os as _os
import secrets as _secrets
import time as _time
from flask import session as _session
from app.services import import_service as _imp
from app.services import jobs as _jobs

_STAGING_TTL_S = 24 * 3600


def _staging_dir():
    d = _os.path.join(current_app.instance_path, 'imports')
    _os.makedirs(d, exist_ok=True)
    return d


def _stage(rows, headers):
    d = _staging_dir()
    now = _time.time()
    for name in _os.listdir(d):                      # drop abandoned uploads
        path = _os.path.join(d, name)
        if now - _os.path.getmtime(path) > _STAGING_TTL_S:
            try:
                _os.remove(path)
            except OSError:
                pass
    token = _secrets.token_hex(16)
    with open(_os.path.join(d, token + '.json'), 'w', encoding='utf-8') as f:
        json.dump({'owner': current_user.id, 'rows': rows, 'headers': headers}, f)
    old = _session.get('import_token')
    _session['import_token'] = token
    if old:
        _unstage(old)


def _staged():
    token = _session.get('import_token') or ''
    if not token.isalnum():
        return None
    try:
        with open(_os.path.join(_staging_dir(), token + '.json'), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if data.get('owner') == current_user.id else None


def _unstage(token):
    if token and token.isalnum():
        try:
            _os.remove(_os.path.join(_staging_dir(), token + '.json'))
        except OSError:
            pass


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
        _stage(rows, headers)
        return render_template('nodes/import_verify.html', mapping=mapping,
                               rows=rows[:200], headers=headers,
                               fields=list(_imp.FIELD_ALIASES.keys()),
                               total=len(rows))
    return render_template('nodes/import.html')


def _check_imported_nodes(node_ids):
    """One collector pass over a batch: SSH in parallel, one database writer.
    Wrong passwords are marked 'auth_failed' so the cards offer Fix login."""
    from app.services.collector import COLLECTOR
    results = COLLECTOR.run_cycle(node_ids=node_ids, force=True)
    return sum(1 for r in results.values() if r['ok'])


@nodes_bp.route('/import/commit', methods=['POST'])
@login_required
@operator_required
def bulk_import_commit():
    staged = _staged()
    if not staged:
        flash('That import expired or was already used. Upload the file again.', 'warning')
        return redirect(url_for('nodes.bulk_import'))
    rows, headers = staged['rows'], staged['headers']
    mapping = {h: (request.form.get(f'map__{h}') or None) for h in headers}
    for k, v in list(mapping.items()):
        if v in ('', 'None'):
            mapping[k] = None
    nodes_data, errors = _imp.to_nodes(
        rows, mapping,
        default_password=request.form.get('default_password', ''),
        host_override=request.form.get('host_override', '').strip())

    existing = {name for (name,) in db.session.query(Node.name)}
    binned = {n.name: n for n in Node.query.filter(Node.is_deleted.is_(True))}
    created, skipped, revived, new_nodes = 0, 0, 0, []
    for rec in nodes_data:
        if rec['name'] in binned:
            # re-adding a deleted node: restore it with the imported details
            # (keeps its history) instead of skipping it
            node = binned.pop(rec['name'])
            pw = rec.pop('password', '')
            for k, v in rec.items():
                if k in Node.__table__.columns.keys() and k not in ('id', 'name'):
                    setattr(node, k, v)
            if pw:
                node.password = pw
            node.is_deleted, node.deleted_at, node.is_active = False, None, True
            node.status, node.login_paused_at, node.login_pause_reason = 'unknown', None, None
            new_nodes.append(node)
            revived += 1
            continue
        if rec['name'] in existing:
            skipped += 1
            continue
        existing.add(rec['name'])
        pw = rec.pop('password', '')
        node = Node(status='unknown', **{k: v for k, v in rec.items()
                    if k in Node.__table__.columns.keys()})
        if pw:
            node.password = pw
        db.session.add(node)
        new_nodes.append(node)
        created += 1
    db.session.commit()
    _unstage(_session.pop('import_token', None))
    log_activity('Bulk import', response=f'{created} created, {revived} restored from the recycle bin, '
                 f'{skipped} skipped', status='success')

    msg = f'Imported {created} node(s), skipped {skipped} already present.'
    if revived:
        msg = (f'Imported {created} node(s) and restored {revived} from the recycle bin with the new details; '
               f'skipped {skipped} already present.')
    if new_nodes:
        from app.services.collector import POOL
        for n in new_nodes:
            POOL.drop(n.id)
        _jobs.run_batches(
            'import-check', f'Checking {len(new_nodes)} imported node(s)', current_user.id,
            [n.id for n in new_nodes], _check_imported_nodes,
            summary=lambda j: f"{j['ok']} of {j['total']} imported node(s) log in over SSH"
                              + (f"; {j['failed']} need attention (see their cards)" if j['failed'] else ''))
        msg += ' Their SSH connection is being checked in the background; progress shows at the bottom of the page.'
    flash(msg, 'success')
    return redirect(url_for('nodes.index'))


@nodes_bp.route('/logins/resume', methods=['POST'])
@login_required
@operator_required
def logins_resume():
    """Retry paused logins: once each, now. If they fail again they pause again."""
    from app.services import login_policy
    from app.services.collector import COLLECTOR
    data = request.get_json(silent=True) or {}
    reason = data.get('reason') if data.get('reason') in login_policy.REASONS else None
    ids = login_policy.resume_nodes(node_ids=data.get('node_ids') or None, reason=reason)
    for nid in ids:
        COLLECTOR.auth_fails[nid] = max(0, int(current_app.config.get('LOGIN_PAUSE_AFTER_FAILURES', 2)) - 1)
    COLLECTOR.kick(ids)
    log_activity('Retry paused logins', response=f'{len(ids)} node(s), one attempt each', status='success')
    return jsonify({'success': True, 'resumed': len(ids),
                    'message': f'Trying {len(ids)} node(s) once each. Any that fail again pause again.'})


@nodes_bp.route('/jobs')
@login_required
def jobs_status():
    """Background jobs of the signed-in user (polled by every page)."""
    return jsonify({'jobs': _jobs.for_user(current_user.id)})


@nodes_bp.route('/jobs/<job_id>')
@login_required
def job_detail(job_id):
    job = _jobs.get(job_id, current_user.id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify(job)


# ── Fleet capabilities (Docker / Node-RED / OpenRemoteGUI) ───────────────────
from app.services import capability_service as _cap


@nodes_bp.route('/capabilities/scan', methods=['POST'])
@login_required
@admin_required            # installs software on nodes: administrators only
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
@admin_required            # installs software on nodes: administrators only
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
