"""Bulk actions on Node Fleet: delete to the recycle bin, restore, permanent
delete, login update for many nodes, and re-adding deleted nodes by import."""
import time
from datetime import datetime

import pytest

from app import create_app, db
from app.models import ActivityLog, MetricsHourly, SystemMetrics
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
        for i in range(5):
            n = Node(name=f'pi-{i}', ip_address=f'10.0.0.{i + 1}', username='pi', status='auth_failed',
                     login_paused_at=datetime.utcnow(), login_pause_reason='password')
            n.password = 'old-pw'
            db.session.add(n)
        db.session.commit()
        u = User(username='op', email='op@fleet.test', role='operator', is_active=True, must_change_password=False)
        u.set_password('Operator-pass-1')
        db.session.add(u)
        db.session.commit()
    yield app


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    return c


@pytest.fixture
def no_ssh(monkeypatch):
    import app.services.collector as col
    seen = []

    def run_cycle(node_ids=None, force=False, manual=False):
        seen.append((tuple(node_ids or ()), manual))
        return {i: {'ok': True} for i in node_ids or ()}
    monkeypatch.setattr(col.COLLECTOR, 'run_cycle', run_cycle)
    return seen


def _ids(app, names=None):
    with app.app_context():
        q = Node.query
        return [n.id for n in q.all() if names is None or n.name in names]


def test_bulk_delete_moves_to_the_recycle_bin_and_restore_brings_back(app, client):
    ids = _ids(app, {'pi-0', 'pi-1', 'pi-2'})
    r = client.post('/nodes/bulk/delete', json={'node_ids': ids})
    assert r.status_code == 200 and r.get_json()['count'] == 3
    with app.app_context():
        assert Node.query.filter_by(is_deleted=True, is_active=False).count() == 3
        assert ActivityLog.query.filter_by(action='Bulk delete (recycle bin)').count() == 1
    r = client.post('/nodes/bulk/restore', json={'all': True}).get_json()
    assert r['count'] == 3
    with app.app_context():
        assert Node.query.filter(Node.is_deleted.is_(True)).count() == 0


def test_bulk_delete_needs_a_selection(client):
    assert client.post('/nodes/bulk/delete', json={'node_ids': []}).status_code == 400


def test_permanent_delete_is_admin_only_typed_and_removes_history(app, client):
    ids = _ids(app, {'pi-3', 'pi-4'})
    with app.app_context():
        for i in ids:
            db.session.add(SystemMetrics(node_id=i, status='online', cpu_percent=5))
            db.session.add(MetricsHourly(node_id=i, hour=datetime(2026, 10, 1), samples=1, online=1))
        db.session.commit()
    client.post('/nodes/bulk/delete', json={'node_ids': ids})
    op = app.test_client()
    op.post('/login', data={'username': 'op', 'password': 'Operator-pass-1'})
    assert op.post('/nodes/bulk/purge', json={'all': True, 'confirm': 'DELETE'}).status_code in (302, 403)
    assert client.post('/nodes/bulk/purge', json={'all': True, 'confirm': 'yes'}).status_code == 400
    live = _ids(app, {'pi-0'})
    r = client.post('/nodes/bulk/purge', json={'node_ids': ids + live, 'confirm': 'DELETE'}).get_json()
    assert r['count'] == 2                                         # only from the recycle bin
    with app.app_context():
        assert Node.query.count() == 3
        assert SystemMetrics.query.count() == 0 and MetricsHourly.query.count() == 0


def test_bulk_login_update_sets_details_resumes_and_checks_once(app, client, no_ssh):
    ids = _ids(app, {'pi-0', 'pi-1'})
    r = client.post('/nodes/bulk/credentials', json={'node_ids': ids, 'username': 'admin2',
                                                     'password': 'N3w-secret', 'ssh_port': '2222'})
    body = r.get_json()
    assert r.status_code == 200 and body['count'] == 2 and body['job_id']
    for _ in range(100):
        if no_ssh:
            break
        time.sleep(0.05)
    assert no_ssh and set(no_ssh[0][0]) == set(ids) and no_ssh[0][1] is True    # one manual check
    with app.app_context():
        for n in Node.query.filter(Node.id.in_(ids)):
            assert (n.username, n.password, n.ssh_port) == ('admin2', 'N3w-secret', 2222)
            assert n.login_paused_at is None
        untouched = Node.query.filter_by(name='pi-2').first()
        assert untouched.username == 'pi' and untouched.password == 'old-pw'
        logs = ' '.join(a.response or '' for a in ActivityLog.query.all())
        assert 'N3w-secret' not in logs                            # never logged


def test_bulk_login_update_keeps_empty_fields_and_validates(app, client, no_ssh):
    ids = _ids(app, {'pi-0'})
    assert client.post('/nodes/bulk/credentials', json={'node_ids': ids}).status_code == 400
    assert client.post('/nodes/bulk/credentials', json={'node_ids': ids, 'ssh_port': '99999'}).status_code == 400
    assert client.post('/nodes/bulk/credentials', json={'node_ids': ids, 'password': 'only-pw'}).status_code == 200
    with app.app_context():
        n = Node.query.filter_by(name='pi-0').first()
        assert n.username == 'pi' and n.password == 'only-pw' and (n.ssh_port or 22) == 22


def test_monitor_cannot_use_bulk_actions(app):
    with app.app_context():
        u = User(username='mon', email='m@fleet.test', role='monitor', is_active=True, must_change_password=False)
        u.set_password('Monitor-pass-1')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'mon', 'password': 'Monitor-pass-1'})
    ids = _ids(app)
    for url, body in (('/nodes/bulk/delete', {'node_ids': ids}),
                      ('/nodes/bulk/credentials', {'node_ids': ids, 'password': 'x'})):
        assert c.post(url, json=body).status_code in (302, 403)
    with app.app_context():
        assert Node.query.filter(Node.is_deleted.is_(True)).count() == 0


def test_importing_a_deleted_node_restores_it_with_the_new_details(app, client, no_ssh):
    import io
    ids = _ids(app, {'pi-0'})
    client.post('/nodes/bulk/delete', json={'node_ids': ids})
    csv_text = 'name,ip,username,password\npi-0,10.9.9.9,newuser,fresh-pw\npi-new,10.9.9.8,pi,pw2\n'
    r = client.post('/nodes/import', data={'file': (io.BytesIO(csv_text.encode()), 'nodes.csv')},
                    content_type='multipart/form-data')
    assert r.status_code == 200
    r = client.post('/nodes/import/commit', data={
        'map__name': 'name', 'map__ip': 'ip_address', 'map__username': 'username',
        'map__password': 'password'}, follow_redirects=True)
    assert b'restored 1 from the recycle bin' in r.data
    with app.app_context():
        n = Node.query.filter_by(name='pi-0').first()
        assert n.id == ids[0]                                      # same node, history kept
        assert not n.is_deleted and n.is_active and n.ip_address == '10.9.9.9'
        assert n.username == 'newuser' and n.password == 'fresh-pw' and n.login_paused_at is None
        assert Node.query.filter_by(name='pi-new').count() == 1




def test_monitoring_auto_refresh_choice_is_kept_on_the_account(app, client):
    page = client.get('/monitoring/').get_data(as_text=True)
    assert 'id="autoRefreshToggle" checked' in page                  # default: on
    assert client.post('/account/prefs', json={'monitoring_auto_refresh': False}).get_json()['success']
    assert 'id="autoRefreshToggle" checked' not in client.get('/monitoring/').get_data(as_text=True)
    client.get('/logout')
    again = app.test_client()                                        # a new sign-in, another browser
    again.post('/login', data={'username': 'admin', 'password': 'admin123'})
    assert 'id="autoRefreshToggle" checked' not in again.get('/monitoring/').get_data(as_text=True)
    op = app.test_client()                                           # other users keep their own
    op.post('/login', data={'username': 'op', 'password': 'Operator-pass-1'})
    assert 'id="autoRefreshToggle" checked' in op.get('/monitoring/').get_data(as_text=True)
    assert again.post('/account/prefs', json={'is_admin': True}).status_code == 400
    assert again.post('/account/prefs', json={'monitoring_auto_refresh': 'no'}).status_code == 400
