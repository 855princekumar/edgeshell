from flask import Blueprint, render_template, jsonify, request
from flask_login import current_user, login_required
from app.utils.timeutil import iso_utc
from app.services import categories as _categories
from app.models.node import Node
from app.models import MetricsHourly, SystemMetrics

monitoring_bp = Blueprint('monitoring', __name__, url_prefix='/monitoring')


@monitoring_bp.route('/')
@login_required
def index():
    nodes = Node.query.filter_by(is_active=True).all()
    return render_template('monitoring/index.html', nodes=nodes)


def _stored(node):
    """A node's last known state from the database, in the poll format."""
    la = (node.load_average or '').split()
    num = lambda i: float(la[i]) if len(la) > i else None   # noqa: E731
    metrics = None
    if node.status == 'online' and node.cpu_percent is not None:
        metrics = {'cpu_percent': node.cpu_percent, 'mem_percent': node.ram_percent,
                   'disk_percent': node.disk_percent, 'temperature': node.temperature,
                   'load_1': num(0), 'load_5': num(1), 'load_15': num(2),
                   'uptime': node.uptime, 'arch': node.arch,
                   'docker_installed': node.docker_installed,
                   'nodered_installed': node.nodered_installed,
                   'collected_at': iso_utc(node.last_seen)}
    return metrics


def _result(node, r=None):
    """One node for the page: live collector result if there is one."""
    from app.services.collector import COLLECTOR
    r = r or COLLECTOR.latest.get(node.id)
    if r:
        return {'success': r['ok'], 'metrics': r['metrics'] if r['ok'] else None,
                'node_name': node.name, 'status': node.status, 'kind': r['kind'],
                'error': None if r['ok'] else r['message'],
                'seconds': r.get('seconds')}
    m = _stored(node)
    return {'success': bool(m), 'metrics': m, 'node_name': node.name, 'status': node.status,
            'kind': 'auth' if node.status == 'auth_failed' else None,
            'error': node.last_error if not m else None, 'seconds': None}


@monitoring_bp.route('/poll', methods=['POST'])
@login_required
def poll():
    """cached: true  returns the background collector's latest results at once.
    otherwise       collects the requested nodes now, in parallel."""
    from app.services.collector import COLLECTOR
    data = request.get_json() or {}
    node_ids = [int(i) for i in data.get('node_ids') or [] if str(i).isdigit()]
    q = Node.query.filter(Node.is_active.is_(True), Node.is_deleted.isnot(True))
    if node_ids:
        q = q.filter(Node.id.in_(node_ids))
    if data.get('cached'):
        return jsonify({'results': {n.id: _result(n) for n in q.all()},
                        'collector': COLLECTOR.status()})
    live = COLLECTOR.run_cycle(node_ids=[n.id for n in q.all()], force=True)
    nodes = {n.id: n for n in q.all()}
    return jsonify({'results': {nid: _result(nodes[nid], r) for nid, r in live.items()
                                if nid in nodes},
                    'collector': COLLECTOR.status()})


@monitoring_bp.route('/categories', methods=['GET', 'POST'])
@login_required
def categories():
    """Dashboard categories. Anyone signed in reads them; administrators edit."""
    if request.method == 'POST':
        if not current_user.is_admin():
            return jsonify({'success': False, 'message': 'Administrator access required.'}), 403
        cats, error = _categories.clean((request.get_json(silent=True) or {}).get('categories'))
        if error:
            return jsonify({'success': False, 'message': error}), 400
        _categories.save(cats)
        from app.services.activity_service import log_activity
        log_activity('Dashboard categories updated', response=', '.join(c['name'] for c in cats))
        return jsonify({'success': True, 'categories': cats})
    return jsonify({'categories': _categories.load(), 'types': _categories.known_types(),
                    'icons': list(_categories.ICONS)})


@monitoring_bp.route('/collector')
@login_required
def collector_status():
    from app.services.collector import COLLECTOR
    return jsonify(COLLECTOR.status())


@monitoring_bp.route('/fleet-summary')
@login_required
def fleet_summary():
    """Aggregate SLA-style stats across all nodes for the dashboard."""
    from app.services import heartbeat_service
    nodes = Node.query.filter_by(is_active=True).all()
    total  = len(nodes)
    online = [n for n in nodes if n.status == 'online']
    offline= [n for n in nodes if n.status != 'online']
    # Availability counts a node as up when SSH reaches it OR a verified MQTT
    # heartbeat arrived in the last seconds (a VPN glitch is not downtime).
    up = [n for n in nodes if n.status == 'online' or heartbeat_service.fresh(n)]
    auth_failed = sum(1 for n in nodes if n.status == 'auth_failed')

    def avg(lst): return round(sum(lst) / len(lst), 1) if lst else 0

    cpus  = [n.cpu_percent  for n in online if n.cpu_percent  is not None]
    mems  = [n.ram_percent  for n in online if n.ram_percent  is not None]
    disks = [n.disk_percent for n in online if n.disk_percent is not None]
    temps = [n.temperature  for n in online if n.temperature  is not None]
    loads = []
    for n in online:
        if n.load_average:
            parts = n.load_average.split()
            if len(parts) >= 3:
                try: loads.append((float(parts[0]), float(parts[1]), float(parts[2])))
                except: pass

    nodes_out = []
    for n in nodes:
        la = n.load_average.split() if n.load_average else []
        nodes_out.append({
            'id': n.id, 'name': n.name, 'ip': n.ip_address,
            'status': n.status, 'last_error': n.last_error, 'node_type': n.node_type,
            'cpu': n.cpu_percent, 'mem': n.ram_percent, 'username': n.username,
            'disk': n.disk_percent, 'temp': n.temperature,
            'load_1':  float(la[0]) if len(la) > 0 else None,
            'load_5':  float(la[1]) if len(la) > 1 else None,
            'load_15': float(la[2]) if len(la) > 2 else None,
            'last_seen': iso_utc(n.last_seen),
            'tags': n.tags, 'arch': getattr(n, 'arch', '-'),
            'heartbeat': n.hb_status, 'heartbeat_fresh': heartbeat_service.fresh(n),
            'heartbeat_lwt_at': iso_utc(n.hb_lwt_at),
        })

    sla = round(len(up) / total * 100, 1) if total else 0

    return jsonify({
        'total': total, 'online': len(online), 'offline': len(offline),
        'auth_failed': auth_failed,
        'categories': _categories.load(),
        'sla_percent': sla,
        'heartbeat_only': len(up) - len(online),
        'avg_cpu':    avg(cpus),
        'avg_mem':    avg(mems),
        'avg_disk':   avg(disks),
        'avg_load_1':  avg([l[0] for l in loads]),
        'avg_load_5':  avg([l[1] for l in loads]),
        'avg_load_15': avg([l[2] for l in loads]),
        'avg_temp':    avg(temps) if temps else None,
        'nodes': nodes_out,
    })


HISTORY_SPANS = {'1h': 3600, '6h': 6 * 3600, '1d': 86400, '7d': 7 * 86400, '30d': 30 * 86400}


@monitoring_bp.route('/history/<int:node_id>')
@login_required
def history(node_id):
    """A node's metrics over a period: every sample up to a day, hourly rollups beyond."""
    from datetime import datetime, timedelta
    node = Node.query.filter(Node.id == node_id, Node.is_deleted.isnot(True)).first_or_404()
    rng = request.args.get('range', '1h')
    if rng not in HISTORY_SPANS:
        return jsonify({'success': False, 'message': 'Unknown period'}), 400
    since = datetime.utcnow() - timedelta(seconds=HISTORY_SPANS[rng])
    if HISTORY_SPANS[rng] <= 86400:
        rows = SystemMetrics.query.filter(SystemMetrics.node_id == node.id, SystemMetrics.timestamp >= since)\
            .order_by(SystemMetrics.timestamp).limit(5000).all()
        points = [{'t': iso_utc(m.timestamp), 'cpu': m.cpu_percent, 'mem': m.ram_percent, 'disk': m.disk_percent,
                   'temp': m.temperature, 'load1': m.load_avg_1,
                   'up': (m.status in (None, 'online')) or bool(m.heartbeat)} for m in rows]
        kind = 'samples'
    else:
        rows = MetricsHourly.query.filter(MetricsHourly.node_id == node.id, MetricsHourly.hour >= since)\
            .order_by(MetricsHourly.hour).all()
        points = [{'t': iso_utc(h.hour), 'cpu': h.cpu_avg, 'mem': h.ram_avg, 'disk': h.disk_avg, 'temp': h.temp_avg,
                   'load1': h.load1_avg,
                   'uptime': round(100.0 * ((h.online or 0) + (h.heartbeat_only or 0)) / h.samples, 1) if h.samples else None}
                  for h in rows]
        kind = 'hours'
    return jsonify({'success': True, 'node': node.name, 'range': rng, 'kind': kind, 'since': iso_utc(since),
                    'points': points})
