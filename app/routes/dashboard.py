from flask import Blueprint, render_template, jsonify, request, send_file
from flask_login import login_required, current_user
from app import limiter
from app.services.activity_service import log_activity
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


# ── Stats export (long-term history as CSV, built in the background) ─────────

@dashboard_bp.route('/dashboard/export', methods=['POST'])
@login_required
@limiter.limit('20 per hour')
def export_start():
    from app.services import history, jobs, categories
    data = request.get_json(silent=True) or {}
    period, grain = data.get('period'), data.get('grain', 'daily')
    category = (data.get('category') or '').strip()
    if period not in history.PERIODS or grain not in history.GRAINS:
        return jsonify({'error': 'Choose a period and a level of detail.'}), 400
    names = {c['name'] for c in categories.load()} | {'Other'}
    if category and category not in names:
        return jsonify({'error': 'Unknown category.'}), 400
    running = [j for j in jobs.for_user(current_user.id)
               if j['kind'] == 'stats-export' and j['state'] == 'running']
    if running:
        return jsonify({'job_id': running[0]['id'], 'already': True})
    owner = current_user.id
    label = history.PERIODS[period][0]
    jid = jobs.run_task('stats-export', f'Stats export: {label.lower()}, {category or "all categories"}',
                        owner, 100, lambda t: history.export_task(t, period, grain, category, owner))
    log_activity('Stats export started', status='success',
                 response=f'{period}, {grain}, {category or "all"}')
    return jsonify({'job_id': jid})


@dashboard_bp.route('/dashboard/export/<job_id>/download')
@login_required
def export_download(job_id):
    from app.services import history, jobs
    job = jobs.get(job_id, current_user.id)
    res = (job or {}).get('result') or {}
    path = history.export_path(res.get('download'), current_user.id)
    if not path:
        return jsonify({'error': 'This export is not available (exports are kept for an hour).'}), 404
    return send_file(path, mimetype='text/csv', as_attachment=True, download_name=res['filename'])
