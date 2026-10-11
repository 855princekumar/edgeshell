from flask import Blueprint, render_template, jsonify
from flask_login import login_required, current_user
from app.models.node import Node
from app.models import ActivityLog, SystemMetrics
from sqlalchemy import desc

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/')
@dashboard_bp.route('/dashboard')
@login_required
def index():
    nodes = Node.query.filter_by(is_active=True).all()
    total = len(nodes)
    online = sum(1 for n in nodes if n.status == 'online')
    offline = sum(1 for n in nodes if n.status == 'offline')
    warning = sum(1 for n in nodes if n.status == 'warning')
    failed = sum(1 for n in nodes if n.status == 'failed')
    healthy = sum(1 for n in nodes if n.status == 'online')

    recent_activities = ActivityLog.query.order_by(desc(ActivityLog.timestamp)).limit(10).all()
    recent_errors = ActivityLog.query.filter_by(status='failure').order_by(desc(ActivityLog.timestamp)).limit(5).all()

    return render_template('dashboard/index.html',
                           total=total, online=online, offline=offline,
                           warning=warning, failed=failed, healthy=healthy,
                           nodes=nodes,
                           recent_activities=recent_activities,
                           recent_errors=recent_errors)


@dashboard_bp.route('/api/dashboard/stats')
@login_required
def stats():
    nodes = Node.query.filter_by(is_active=True).all()
    return jsonify({
        'total': len(nodes),
        'online': sum(1 for n in nodes if n.status == 'online'),
        'offline': sum(1 for n in nodes if n.status == 'offline'),
        'warning': sum(1 for n in nodes if n.status == 'warning'),
        'failed': sum(1 for n in nodes if n.status == 'failed'),
        'nodes': [n.to_dict() for n in nodes]
    })
