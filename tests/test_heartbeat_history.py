"""Heartbeat history and CSV export through EdgeShell (data from SenseHive), and the
monitoring page's metrics history."""
from datetime import datetime, timedelta

import pytest
import requests
from test_heartbeat import app, login  # noqa: F401  (fixture and helper)

from app import db
from app.models import MetricsHourly, SystemMetrics
from app.models.node import Node
from app.models.user import User


class Resp:
    def __init__(self, status=200, data=None, text=''):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.status_code)


@pytest.fixture
def sensehive(monkeypatch):
    """A fake SenseHive: records every call, answers like the real API."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({'url': url, 'params': params, 'headers': headers})
        if url.endswith('/api/heartbeat/history'):
            if params['node'] == 'pi-02':
                return Resp(404, {'error': 'Unknown node'})
            return Resp(200, {'node': params['node'], 'range': params['range'], 'kind': 'beats', 'since': 1, 'until': 2,
                              'points': [[1.5, 1, 0.2]], 'interval': 10, 'breaks': [], 'received': 1, 'lag_ms': 200,
                              'longest_break': 0, 'events': [], 'uptime': 99.5, 'secret_field': 'dropped',
                              'current_break': {'from': 0.5, 'to': None, 'seconds': 1.5, 'status': 'offline', 'lwt_at': 0.9}})
        if url.endswith('/api/heartbeat/export.csv'):
            rows = ''.join(f'{n},2026-10-16T10:00:00Z,1760608800.000,hb,210\n' for n in params['nodes'].split(','))
            return Resp(200, text='node,time_utc,unix_time,kind,delay_ms\n' + rows)
        return Resp(404)
    monkeypatch.setattr(requests, 'get', fake_get)
    return calls


def node_id(app, name):
    with app.app_context():
        return Node.query.filter_by(name=name).first().id


def test_history_comes_from_sensehive_with_the_token(app, sensehive):  # noqa: F811
    c = login(app)
    r = c.get(f'/nodes/{node_id(app, "pi-00")}/heartbeat/history?range=1d')
    d = r.get_json()
    assert r.status_code == 200 and d['success'] and d['uptime'] == 99.5 and d['range'] == '1d'
    assert 'secret_field' not in d                             # only the known fields pass through
    assert d['current_break']['status'] == 'offline'           # the outage still going on reaches the page
    call = sensehive[-1]
    assert call['url'] == 'http://sensehive:5000/api/heartbeat/history'
    assert call['params'] == {'node': 'pi-00', 'range': '1d'}
    assert call['headers']['Authorization'] == 'Bearer ' + 't' * 40


def test_history_input_and_failures(app, sensehive, monkeypatch):  # noqa: F811
    c = login(app)
    nid = node_id(app, 'pi-00')
    assert c.get(f'/nodes/{nid}/heartbeat/history?range=1y').status_code == 400
    assert c.get('/nodes/99999/heartbeat/history').status_code == 404
    assert c.get(f'/nodes/{node_id(app, "pi-02")}/heartbeat/history').status_code == 404    # SenseHive has none
    monkeypatch.setattr(requests, 'get', lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
    r = c.get(f'/nodes/{nid}/heartbeat/history')
    assert r.status_code == 503 and 'not reachable' in r.get_json()['message']
    app.config['SENSEHIVE_API_TOKEN'] = ''
    assert c.get(f'/nodes/{nid}/heartbeat/history').status_code == 503


def test_history_needs_a_login(app, sensehive):  # noqa: F811
    r = app.test_client().get(f'/nodes/{node_id(app, "pi-00")}/heartbeat/history')
    assert r.status_code in (302, 401)
    assert not sensehive


def test_csv_export_for_one_or_several_nodes(app, sensehive):  # noqa: F811
    c = login(app)
    ids = [node_id(app, n) for n in ('pi-00', 'pi-01')]
    one = c.get(f'/nodes/heartbeat/export.csv?ids={ids[0]}&what=beats&period=1d')
    assert one.status_code == 200 and one.mimetype == 'text/csv'
    assert 'heartbeats-beats-pi-00-1d.csv' in one.headers['Content-Disposition']
    assert one.get_data(as_text=True).splitlines()[1].startswith('pi-00,')
    both = c.get(f'/nodes/heartbeat/export.csv?ids={ids[0]},{ids[1]}&what=minutes&period=7d')
    assert both.status_code == 200 and 'heartbeats-minutes-2-nodes-7d.csv' in both.headers['Content-Disposition']
    assert sensehive[-1]['params']['nodes'] == 'pi-00,pi-01' and sensehive[-1]['params']['what'] == 'minutes'
    p = sensehive[-1]['params']
    assert p['until'] - p['since'] == 7 * 86400
    from app.models import ActivityLog
    with app.app_context():
        assert ActivityLog.query.filter_by(action='Export heartbeats').count() == 2


def test_csv_export_input_and_roles(app, sensehive):  # noqa: F811
    c = login(app)
    nid = node_id(app, 'pi-00')
    for q in ('ids=&what=beats&period=1d', f'ids={nid}&what=passwords&period=1d', f'ids={nid}&what=beats&period=1y',
              'ids=abc&what=beats&period=1d'):
        assert c.get('/nodes/heartbeat/export.csv?' + q).status_code == 400, q
    assert c.get('/nodes/heartbeat/export.csv?ids=99999&what=beats&period=1d').status_code == 404
    with app.app_context():
        m = User(username='mon', email='mon@fleet.test', role='monitor', is_active=True, must_change_password=False)
        m.set_password('Monitor-pass-1')
        db.session.add(m)
        db.session.commit()
    mon = login(app, 'mon', 'Monitor-pass-1')
    assert mon.get(f'/nodes/heartbeat/export.csv?ids={nid}&what=beats&period=1d').status_code == 200   # one node: any role
    several = mon.get(f'/nodes/heartbeat/export.csv?ids={nid},{node_id(app, "pi-01")}&what=beats&period=1d')
    assert several.status_code == 403


def test_monitoring_history_returns_samples_and_hourly_averages(app):  # noqa: F811
    nid = node_id(app, 'pi-00')
    now = datetime.utcnow()
    with app.app_context():
        for i in range(30):
            db.session.add(SystemMetrics(node_id=nid, timestamp=now - timedelta(minutes=i), cpu_percent=10 + i,
                                         ram_percent=40, disk_percent=60, temperature=50, load_avg_1=0.5,
                                         status='online' if i != 3 else 'offline'))
        for h in range(48):
            db.session.add(MetricsHourly(node_id=nid, hour=(now - timedelta(hours=h + 2)).replace(minute=0, second=0, microsecond=0),
                                         samples=60, online=60 if h % 10 else 30, cpu_avg=20, ram_avg=40, disk_avg=60))
        db.session.commit()
    c = login(app)
    d = c.get(f'/monitoring/history/{nid}?range=1h').get_json()
    assert d['success'] and d['kind'] == 'samples' and len(d['points']) == 30
    assert {'t', 'cpu', 'mem', 'disk', 'temp', 'load1', 'up'} <= set(d['points'][0])
    assert sum(1 for p in d['points'] if not p['up']) == 1
    week = c.get(f'/monitoring/history/{nid}?range=7d').get_json()
    assert week['kind'] == 'hours' and len(week['points']) == 48
    assert {p['uptime'] for p in week['points']} == {50.0, 100.0}
    assert c.get(f'/monitoring/history/{nid}?range=1y').status_code == 400
    assert c.get('/monitoring/history/99999').status_code == 404


def test_the_monitoring_page_reads_the_same_fields_the_server_sends(app):  # noqa: F811
    page = login(app).get('/monitoring/').get_data(as_text=True)
    assert 'd.points' in page and "p[key]" in page                        # renderer reads the server's keys
    assert 'h.collected_at' not in page and 'h.cpu_percent' not in page and 'd.history' not in page
    assert 'data-range="30d"' in page and 'modal-dialog-scrollable' in page
