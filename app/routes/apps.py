"""Apps on the edge (ADR 0017, docs/APPS.md).

Roles: operators see the packages, the nodes' versions and the audit, and
read a node's apps again; administrators upload packages, roll them out,
roll back, switch versions, clean up, remove apps and remove packages from
the bucket after the audit.
"""
from flask import Blueprint, jsonify, render_template, request, send_file
from flask_login import current_user, login_required

from app import db, limiter
from app.models.apps import AppAction, AppPackage
from app.models.node import Node
from app.services import app_service as svc
from app.services.activity_service import log_activity
from app.services.rbac import admin_required, operator_required

apps_bp = Blueprint('apps', __name__, url_prefix='/apps')


def _fail(message, code=400):
    return jsonify({'success': False, 'message': message}), code


def _body():
    return request.get_json(silent=True) or {}


def _nodes(ids):
    try:
        ids = [int(i) for i in ids or []]
    except (TypeError, ValueError):
        return []
    return Node.query.filter(Node.id.in_(ids), Node.is_deleted.isnot(True)).all() if ids else []


@apps_bp.route('/')
@login_required
@operator_required
def index():
    return render_template('apps/index.html', min_stable=svc.ES.MIN_STABLE, esapp_version=svc.ES.ESAPP_VERSION)


# ------------------------------------------------------------------ packages (the bucket)
@apps_bp.route('/api/packages')
@login_required
@operator_required
def packages():
    rows = AppPackage.query.order_by(AppPackage.name, AppPackage.uploaded_at.desc()).all()
    return jsonify({'packages': [p.to_dict() for p in rows]})


@apps_bp.route('/api/packages', methods=['POST'])
@login_required
@admin_required
@limiter.limit('60 per hour')
def upload():
    f = request.files.get('package')
    if not f or not f.filename:
        return _fail('Choose a .esapp package (build one with: python3 esapp.py pack FOLDER)')
    try:
        pkg, created = svc.store(f.stream, current_user.username, request.form.get('note'))
    except svc.Refused as e:
        return _fail(e.text)
    log_activity('App package upload', command='%s %s' % (pkg.name, pkg.version),
                 response='sha256 %s, %d bytes%s' % (pkg.sha256[:16], pkg.size, '' if created else ' (same build again)'))
    return jsonify({'success': True, 'created': created, 'package': pkg.to_dict()})


@apps_bp.route('/api/packages/<int:pid>/download')
@login_required
@admin_required
def download(pid):
    pkg = db.session.get(AppPackage, pid)
    if not pkg or pkg.removed_at or not svc.verify(pkg):
        return _fail('Not in the bucket (or the file changed)', 404)
    log_activity('App package download', command='%s %s' % (pkg.name, pkg.version))
    return send_file(svc.package_path(pkg), as_attachment=True, download_name=pkg.filename,
                     mimetype='application/gzip')


@apps_bp.route('/api/packages/<int:pid>/remove', methods=['POST'])
@login_required
@admin_required
def remove_package(pid):
    pkg = db.session.get(AppPackage, pid)
    if not pkg or pkg.removed_at:
        return _fail('Not in the bucket', 404)
    if _body().get('confirm') != '%s %s' % (pkg.name, pkg.version):
        return _fail('Type the name and version to confirm')
    if AppAction.query.filter_by(package_id=pkg.id, status='running').first():
        return _fail('A rollout of this package is still running')
    svc.remove_from_bucket(pkg, current_user.username)
    log_activity('App package removed from the bucket', command='%s %s' % (pkg.name, pkg.version),
                 response='the upload record and every rollout stay in the audit')
    return jsonify({'success': True})


# ------------------------------------------------------------------ rollouts and actions
@apps_bp.route('/api/rollout', methods=['POST'])
@login_required
@admin_required
def rollout():
    d = _body()
    pkg = db.session.get(AppPackage, int(d.get('package_id') or 0))
    if not pkg or pkg.removed_at:
        return _fail('Pick a package that is in the bucket')
    nodes = _nodes(d.get('node_ids'))
    if not nodes:
        return _fail('Select at least one node')
    try:
        canary, stop = int(d.get('canary', 1)), int(d.get('stop_after_pct', 20))
    except (TypeError, ValueError):
        return _fail('Canary and stop threshold must be numbers')
    jid, aid = svc.start_rollout(pkg, nodes, current_user.id, current_user.username, canary, stop)
    log_activity('App rollout started', command='%s %s to %d node(s), canary %d' % (pkg.name, pkg.version, len(nodes), canary))
    return jsonify({'success': True, 'job_id': jid, 'action_id': aid})


@apps_bp.route('/api/action', methods=['POST'])
@login_required
@admin_required
def action():
    d = _body()
    kind, name = d.get('kind'), (d.get('app') or '').strip()
    nodes = _nodes(d.get('node_ids'))
    if kind not in svc.KINDS:
        return _fail('Unknown action')
    if not svc.ES.NAME_RE.match(name):
        return _fail('Unknown app')
    if not nodes:
        return _fail('Select at least one node')
    versions = [str(v) for v in d.get('versions') or []]
    version = d.get('version') or None
    if any(not svc.ES.VERSION_RE.match(v) for v in versions + ([version] if version else [])):
        return _fail('Not a version')
    if kind == 'activate' and not version:
        return _fail('Which version?')
    if kind == 'uninstall' and d.get('confirm') != name:
        return _fail('Type the app name to confirm its removal')
    jid, aid = svc.start_action(kind, name, nodes, current_user.id, current_user.username, version, versions,
                                bool(d.get('keep_data')))
    log_activity('App %s started' % kind, command='%s on %d node(s)' % (name, len(nodes)))
    return jsonify({'success': True, 'job_id': jid, 'action_id': aid})


@apps_bp.route('/api/scan', methods=['POST'])
@login_required
@operator_required
def scan():
    nodes = _nodes(_body().get('node_ids')) or Node.query.filter(Node.is_deleted.isnot(True), Node.status == 'online').all()
    if not nodes:
        return _fail('No node is reachable now')
    return jsonify({'success': True, 'job_id': svc.start_scan(nodes, current_user.id)})


# ------------------------------------------------------------------ what is where, and the audit
@apps_bp.route('/api/nodes')
@login_required
@operator_required
def nodes():
    from app.utils.timeutil import iso_utc
    rows = Node.query.filter(Node.is_deleted.isnot(True)).order_by(Node.name).all()
    return jsonify({'nodes': [{'id': n.id, 'name': n.name, 'status': n.status, 'esapp': n.esapp_version,
                               'checked_at': iso_utc(n.apps_checked_at), 'apps': svc.inventory_of(n)} for n in rows]})


@apps_bp.route('/api/actions')
@login_required
@operator_required
def actions():
    rows = AppAction.query.order_by(AppAction.started_at.desc()).limit(200).all()
    return jsonify({'actions': [a.to_dict() for a in rows]})


@apps_bp.route('/api/esapp.py')
@login_required
@operator_required
def esapp_tool():
    """The packaging tool (the same file the nodes run): python3 esapp.py pack FOLDER."""
    return send_file(svc.ESAPP_PATH, as_attachment=True, download_name='esapp.py', mimetype='text/x-python')
