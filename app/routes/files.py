import os
import json
import time
from flask import Blueprint, render_template, request, jsonify, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from app import db
from app.models.node import Node
from app.models import Deployment
from app.services.ansible_service import deploy_file
from app.services.rbac import operator_required as operator_required_page
from app.services.activity_service import log_activity
from datetime import datetime
from functools import wraps

files_bp = Blueprint('files', __name__, url_prefix='/files')

# All file types allowed - the node filesystem is the limit, not EdgeShell
def allowed_file(filename):
    return True


def operator_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.can_upload():
            return jsonify({'error': 'Access denied'}), 403
        return f(*args, **kwargs)
    return decorated


@files_bp.route('/')
@login_required
@operator_required_page
def index():
    nodes = Node.query.filter_by(is_active=True).all()
    deployments = Deployment.query.order_by(Deployment.timestamp.desc()).limit(50).all()
    return render_template('files/index.html', nodes=nodes, deployments=deployments)


@files_bp.route('/upload', methods=['POST'])
@login_required
@operator_required
def upload():
    # Accept both the new multi-file field ('files') and the legacy single ('file').
    files = request.files.getlist('files') or request.files.getlist('file')
    files = [f for f in files if f and f.filename]
    node_ids = request.form.getlist('node_ids')
    # Files land only in ~/Downloads/edgeshell on each node (a folder inside it may be given)
    from app.services.ssh_service import clean_subfolder, UPLOAD_ROOT, UPLOAD_REFUSED
    try:
        sub = clean_subfolder(request.form.get('subfolder', request.form.get('target_path', '')))
    except ValueError:
        return jsonify({'error': UPLOAD_REFUSED}), 400
    target_path = '~/' + UPLOAD_ROOT + ('/' + sub if sub else '')

    if not files:
        return jsonify({'error': 'No files provided'}), 400
    if not node_ids:
        return jsonify({'error': 'No nodes selected'}), 400

    nodes = Node.query.filter(Node.id.in_(node_ids), Node.is_active == True).all()
    if not nodes:
        return jsonify({'error': 'No valid nodes found'}), 400

    upload_dir = current_app.config['UPLOAD_FOLDER']
    start = time.time()

    # results[filename][node_name] = {success, message}
    per_file = {}
    total_ok = total_fail = 0
    for f in files:
        filename = secure_filename(f.filename)
        local_path = os.path.join(upload_dir, filename)
        f.save(local_path)
        file_results = deploy_file(nodes, local_path, sub)
        per_file[filename] = file_results
        ok = sum(1 for r in file_results.values() if r.get('success'))
        total_ok += ok
        total_fail += len(file_results) - ok

    duration = round(time.time() - start, 2)
    status = 'success' if total_fail == 0 else ('partial' if total_ok > 0 else 'failed')

    deployment = Deployment(
        user=current_user.username,
        filename=', '.join(per_file.keys()),
        target_path=target_path,
        nodes=json.dumps([n.name for n in nodes]),
        status=status,
        success_count=total_ok,
        failure_count=total_fail,
        result=json.dumps(per_file),
        duration=duration)
    db.session.add(deployment)
    db.session.commit()

    log_activity('File Deploy',
                 command=f'{len(files)} file(s) -> {target_path} on {len(nodes)} node(s)',
                 response=json.dumps(per_file)[:1000],
                 status='success' if total_fail == 0 else 'failure',
                 duration=duration)

    return jsonify({
        'success': True,
        'files': list(per_file.keys()),
        'results': per_file,
        'success_count': total_ok,
        'failure_count': total_fail,
        'duration': duration,
        'deployment_id': deployment.id,
    })


@files_bp.route('/history')
@login_required
def history():
    deployments = Deployment.query.order_by(Deployment.timestamp.desc()).limit(100).all()
    return jsonify([d.to_dict() for d in deployments])
