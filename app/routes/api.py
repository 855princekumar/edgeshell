import sys
import flask
from flask import Blueprint, current_app, jsonify, request
from flask_login import login_required, current_user
from app.models.node import Node
from app.models import ActivityLog, NodeLog
from sqlalchemy import desc

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')


@api_bp.route('/nodes')
@login_required
def nodes():
    nodes = Node.query.filter_by(is_active=True).all()
    return jsonify([n.to_dict() for n in nodes])


@api_bp.route('/nodes/<int:node_id>')
@login_required
def node(node_id):
    node = Node.query.get_or_404(node_id)
    return jsonify(node.to_dict())


@api_bp.route('/activity')
@login_required
def activity():
    limit = request.args.get('limit', 50, type=int)
    logs = ActivityLog.query.order_by(desc(ActivityLog.timestamp)).limit(limit).all()
    return jsonify([l.to_dict() for l in logs])


@api_bp.route('/status')
@login_required
def status():
    nodes = Node.query.filter_by(is_active=True).all()
    return jsonify({
        'platform': 'EdgeShell',
        'version': current_app.config.get('VERSION', 'dev'),
        'python_version': sys.version.split()[0],
        'flask_version': flask.__version__,
        'nodes': {
            'total': len(nodes),
            'online': sum(1 for n in nodes if n.status == 'online'),
            'offline': sum(1 for n in nodes if n.status == 'offline'),
        }
    })
