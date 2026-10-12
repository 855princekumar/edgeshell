from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required
from app.utils.timeutil import iso_utc
from app.models.node import Node
from app.models import SystemMetrics
from app.services.ssh_service import collect_metrics
from app import db
from datetime import datetime

monitoring_bp = Blueprint('monitoring', __name__, url_prefix='/monitoring')


@monitoring_bp.route('/')
@login_required
def index():
    nodes = Node.query.filter_by(is_active=True).all()
    return render_template('monitoring/index.html', nodes=nodes)


@monitoring_bp.route('/poll', methods=['POST'])
@login_required
def poll():
    data     = request.get_json() or {}
    node_ids = data.get('node_ids', [])
    nodes    = Node.query.filter(Node.id.in_(node_ids), Node.is_active == True).all() \
               if node_ids else Node.query.filter_by(is_active=True).all()

    results = {}
    for node in nodes:
        m = collect_metrics(node)
        if m:
            node.cpu_percent  = m.get('cpu_percent')
            node.ram_percent  = m.get('mem_percent')
            node.disk_percent = m.get('disk_percent')
            node.load_average = f"{m.get('load_1',0)} {m.get('load_5',0)} {m.get('load_15',0)}"
            node.temperature  = m.get('temperature')
            node.status       = 'online'
            node.last_seen    = datetime.utcnow()

            sm = SystemMetrics(
                node_id       = node.id,
                cpu_percent   = m.get('cpu_percent'),
                ram_percent   = m.get('mem_percent'),
                disk_percent  = m.get('disk_percent'),
                load_avg_1    = m.get('load_1'),
                load_avg_5    = m.get('load_5'),
                load_avg_15   = m.get('load_15'),
                uptime_seconds= m.get('uptime_seconds'),
            )
            db.session.add(sm)
            results[node.id] = {'success': True, 'metrics': m, 'node_name': node.name}
        else:
            node.status = 'offline'
            results[node.id] = {'success': False, 'node_name': node.name}

    db.session.commit()
    return jsonify({'results': results})


@monitoring_bp.route('/fleet-summary')
@login_required
def fleet_summary():
    """Aggregate SLA-style stats across all nodes for the dashboard."""
    nodes = Node.query.filter_by(is_active=True).all()
    total  = len(nodes)
    online = [n for n in nodes if n.status == 'online']
    offline= [n for n in nodes if n.status != 'online']

    if not online:
        return jsonify({'total': total, 'online': 0, 'offline': total,
                        'sla_percent': 0, 'avg_cpu': 0, 'avg_mem': 0,
                        'avg_disk': 0, 'avg_load_1': 0, 'avg_load_5': 0,
                        'avg_load_15': 0, 'avg_temp': None, 'nodes': []})

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
            'status': n.status, 'cpu': n.cpu_percent, 'mem': n.ram_percent,
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
