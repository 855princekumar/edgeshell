"""Long-term history: per-minute samples, hourly rollups and the CSV stats export."""
import csv
import io
import time
from datetime import datetime, timedelta

import pytest

from app import create_app, db
from app.models import MetricsHourly, SystemMetrics
from app.models.node import Node
from app.models.user import User


@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        db.drop_all()
        db.create_all()
        from app import _seed_defaults
        _seed_defaults()
        a = User.query.filter_by(username='admin').first()
        a.must_change_password = False
        a.email = 'o@fleet.test'
        for name, t in (('pi-1', 'pi-node'), ('gw-1', 'gateway'), ('pc-1', 'mini-pc')):
            n = Node(name=name, ip_address='10.0.0.1', username='pi', node_type=t, status='online')
            n.password = 'x'
            db.session.add(n)
        db.session.commit()
    yield app


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    return c


def _ids(app):
    with app.app_context():
        return {n.name: n.id for n in Node.query.all()}


def _fill(app, hours=3):
    """Samples every 10 minutes: the Pi node is up, then reboots; the
    gateway is down for half the time; the mini PC has a login problem."""
    ids = _ids(app)
    start = (datetime.utcnow() - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    with app.app_context():
        for k in range(hours * 6):
            ts = start + timedelta(minutes=10 * k)
            up = 3600 + k * 600 if k != 4 else 30                  # reboot at sample 4
            db.session.add(SystemMetrics(node_id=ids['pi-1'], timestamp=ts, status='online',
                                         cpu_percent=20 + k, ram_percent=40, disk_percent=50,
                                         temperature=55, load_avg_1=0.5, uptime_seconds=up,
                                         network_rx=1000 * k, network_tx=500 * k))
            down = k % 2 == 1
            db.session.add(SystemMetrics(node_id=ids['gw-1'], timestamp=ts,
                                         status='offline' if down else 'online',
                                         cpu_percent=None if down else 10))
            db.session.add(SystemMetrics(node_id=ids['pc-1'], timestamp=ts, status='auth_failed'))
        db.session.commit()
    return start, ids


def test_samples_record_every_node_whatever_its_state(app):
    from app.services import history
    with app.app_context():
        nodes = Node.query.order_by(Node.name).all()
        mini_pc, gateway, pi_node = nodes
        mini_pc.status = 'auth_failed'
        gateway.status = 'offline'
        latest = {pi_node.id: {'ok': True, 'at': datetime.utcnow(),
                              'metrics': {'cpu_percent': 12.5, 'mem_percent': 30, 'disk_percent': 40,
                                          'temperature': 51.2, 'load_1': 0.3, 'uptime_seconds': 999}},
                  gateway.id: {'ok': True, 'at': datetime.utcnow() - timedelta(minutes=10), 'metrics': {}}}
        rows = {r.node_id: r for r in history.sample_rows(nodes, latest, fresh_s=120)}
        assert rows[pi_node.id].status == 'online' and rows[pi_node.id].temperature == 51.2
        assert rows[gateway.id].status == 'offline' and rows[gateway.id].cpu_percent is None   # stale result
        assert rows[mini_pc.id].status == 'auth_failed'


def test_rollup_measures_uptime_reboots_and_traffic(app):
    from app.services import history
    start, ids = _fill(app, hours=3)
    with app.app_context():
        assert history.roll_up() >= 3
        assert history.roll_up() == 0                                   # nothing new: idempotent
        first = MetricsHourly.query.filter_by(node_id=ids['pi-1'], hour=start).one()
        assert first.samples == 6 and first.online == 6 and first.reboots == 1
        assert first.cpu_max == 25 and first.net_rx_bytes > 0          # counter reset not negative
        gw = MetricsHourly.query.filter_by(node_id=ids['gw-1'], hour=start).one()
        assert (gw.online, gw.offline) == (3, 3)
        mini_pc = MetricsHourly.query.filter_by(node_id=ids['pc-1'], hour=start).one()
        assert mini_pc.auth_failed == 6 and mini_pc.cpu_avg is None


def _export(app, client, **body):
    from app.services import jobs
    r = client.post('/dashboard/export', json=body)
    assert r.status_code == 200, r.get_json()
    jid = r.get_json()['job_id']
    with app.app_context():
        uid = User.query.filter_by(username='admin').first().id
    for _ in range(200):
        j = jobs.get(jid, uid)
        if j['state'] != 'running':
            break
        time.sleep(0.05)
    assert j['state'] == 'done', j
    return jid, j


def test_export_builds_a_categorised_csv_in_the_background(app, client):
    _fill(app, hours=3)
    jid, j = _export(app, client, period='week', grain='summary')
    assert j['done'] == j['total'] == 100 and j['result']['nodes'] == 3
    r = client.get(f'/dashboard/export/{jid}/download')
    assert r.status_code == 200 and r.mimetype == 'text/csv'
    assert 'attachment' in r.headers['Content-Disposition'] and 'summary' in r.headers['Content-Disposition']
    rows = {x['node_name']: x for x in csv.DictReader(io.StringIO(r.get_data(as_text=True)))}
    assert rows['pi-1']['category'] == 'Pi Nodes' and float(rows['pi-1']['uptime_percent']) == 100
    assert rows['gw-1']['category'] == 'Edge Gateways' and float(rows['gw-1']['uptime_percent']) == 50
    assert rows['pc-1']['category'] == 'Mini PCs' and float(rows['pc-1']['uptime_percent']) == 0
    assert int(rows['pi-1']['reboots']) == 1


def test_export_by_category_and_hour(app, client):
    _fill(app, hours=3)
    jid, j = _export(app, client, period='day', grain='hourly', category='Edge Gateways')
    body = client.get(f'/dashboard/export/{jid}/download').get_data(as_text=True)
    rows = list(csv.DictReader(io.StringIO(body)))
    assert rows and {x['node_name'] for x in rows} == {'gw-1'}
    assert len(rows) >= 3                                                # one per hour


def test_export_rejects_bad_input_and_other_users(app, client):
    assert client.post('/dashboard/export', json={'period': 'decade'}).status_code == 400
    assert client.post('/dashboard/export', json={'period': 'week', 'category': 'Nope'}).status_code == 400
    _fill(app, hours=1)
    jid, _ = _export(app, client, period='day', grain='daily')
    with app.app_context():
        u = User(username='mon', email='mon@fleet.test', role='monitor', is_active=True,
                 must_change_password=False)
        u.set_password('Monitor-pass-1')
        db.session.add(u)
        db.session.commit()
    other = app.test_client()
    other.post('/login', data={'username': 'mon', 'password': 'Monitor-pass-1'})
    assert other.get(f'/dashboard/export/{jid}/download').status_code == 404   # not their export
