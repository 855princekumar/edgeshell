from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required
from app.utils.timeutil import iso_utc
from app.models.node import Node
from app.models import SystemMetrics

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


@monitoring_bp.route('/collector')
@login_required
def collector_status():
    from app.services.collector import COLLECTOR
    return jsonify(COLLECTOR.status())


@monitoring_bp.route('/fleet-summary')
@login_required
def fleet_summary():
    """Aggregate SLA-style stats across all nodes for the dashboard."""
    nodes = Node.query.filter_by(is_active=True).all()
    total  = len(nodes)
    online = [n for n in nodes if n.status == 'online']
    offline= [n for n in nodes if n.status != 'online']
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
            'status': n.status, 'last_error': n.last_error,
            'cpu': n.cpu_percent, 'mem': n.ram_percent, 'username': n.username,
            'disk': n.disk_percent, 'temp': n.temperature,
            'load_1':  float(la[0]) if len(la) > 0 else None,
            'load_5':  float(la[1]) if len(la) > 1 else None,
            'load_15': float(la[2]) if len(la) > 2 else None,
            'last_seen': iso_utc(n.last_seen),
            'tags': n.tags, 'arch': getattr(n, 'arch', '-'),
        })

    sla = round(len(online) / total * 100, 1) if total else 0

    return jsonify({
        'total': total, 'online': len(online), 'offline': len(offline),
        'auth_failed': auth_failed,
        'sla_percent': sla,
        'avg_cpu':    avg(cpus),
        'avg_mem':    avg(mems),
        'avg_disk':   avg(disks),
        'avg_load_1':  avg([l[0] for l in loads]),
        'avg_load_5':  avg([l[1] for l in loads]),
        'avg_load_15': avg([l[2] for l in loads]),
        'avg_temp':    avg(temps) if temps else None,
        'nodes': nodes_out,
    })


@monitoring_bp.route('/history/<int:node_id>')
@login_required
def history(node_id):
    rows = SystemMetrics.query.filter_by(node_id=node_id)\
               .order_by(SystemMetrics.timestamp.desc()).limit(60).all()
    return jsonify([{
        'timestamp': iso_utc(m.timestamp),
        'cpu': m.cpu_percent, 'mem': m.ram_percent, 'disk': m.disk_percent,
        'load_1': m.load_avg_1, 'load_5': m.load_avg_5, 'load_15': m.load_avg_15,
    } for m in reversed(rows)])
