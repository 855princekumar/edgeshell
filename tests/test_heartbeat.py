"""MQTT heartbeat in EdgeShell: provisioning never sends the master key, the
fleet config is always one the agent accepts, SenseHive's state lands on the
right nodes, uptime counts a node as up when SSH or a verified heartbeat says
so, and only administrators install agents, push settings or open SenseHive."""
import os
import sys
from datetime import datetime, timedelta

import pytest

from app import create_app, db, limiter
from app.models.node import Node
from app.models.user import User

MASTER = os.urandom(32).hex()

# Same values as mqtt-heartbeat-service/tests/test_protocol.py and SenseHive.
VECTORS = {'kid': 'ebb13644074fabc3', 'topic_3000000': '3abd08/4a0e86dbcd37698b1225a11a'}


@pytest.fixture
def app():
    app = create_app('testing')
    app.config.update(HEARTBEAT_MASTER_KEY=MASTER, SENSEHIVE_URL='http://sensehive:5000',
                      SENSEHIVE_API_TOKEN='t' * 40, SENSEHIVE_PUBLIC_PATH='/sensehive/')
    limiter.reset()
    with app.app_context():
        db.drop_all()
        db.create_all()
        from app import _seed_defaults
        _seed_defaults()
        a = User.query.filter_by(username='admin').first()
        a.must_change_password, a.email = False, 'owner@fleet.test'
        u = User(username='op', email='op@fleet.test', role='operator', is_active=True, must_change_password=False)
        u.set_password('Operator-pass-1')
        db.session.add(u)
        for i in range(3):
            db.session.add(Node(name=f'pi-0{i}', ip_address=f'10.0.0.{i + 1}', username='pi', status='online'))
        db.session.commit()
    yield app


def login(app, user='admin', pw='admin123'):
    c = app.test_client()
    c.post('/login', data={'username': user, 'password': pw})
    return c


def test_the_bundled_protocol_matches_the_agent_and_sensehive(app):
    from app.services import heartbeat_service as hb
    hp = hb.protocol()
    m = bytes(range(32))
    assert hp.kid_for(m, 'pi-01').hex() == VECTORS['kid']
    assert hp.topic(hp.topic_key(m), 3_000_000) == VECTORS['topic_3000000']


def test_a_node_gets_its_own_keys_and_never_the_master_key(app):
    from app.services import heartbeat_service as hb
    with app.app_context():
        n = Node.query.filter_by(name='pi-01').first()
        env = hb.agent_env(n)
        assert MASTER not in env and 'HB_NODE=pi-01' in env
        other = hb.agent_env(Node.query.filter_by(name='pi-02').first())
        key = [l for l in env.splitlines() if l.startswith('HB_NODE_KEY=')][0]
        assert key not in other                                   # every node has a different key


def test_the_pushed_config_is_always_one_the_agent_accepts(app):
    from app.services import heartbeat_service as hb
    sys.path.insert(0, hb.AGENT_DIR)
    import heartbeat_agent as agent
    with app.app_context():
        s, err = hb.validate_settings({'broker': 'mqtt.example.org', 'port': 8884, 'tls': True, 'interval': 15})
        assert err is None
        parsed = agent.parse_settings(hb.config_yaml(s))
        assert {k: parsed[k] for k in ('broker', 'port', 'tls', 'interval')} == {
            'broker': 'mqtt.example.org', 'port': 8884, 'tls': True, 'interval': 15}
        for bad in ({'broker': 'evil/topic'}, {'broker': 'a b'}, {'port': 0}, {'interval': 1}, {'port': 'x'}):
            assert hb.validate_settings(bad)[1], bad


def test_sensehive_state_lands_on_the_right_nodes(app):
    from app.models.api import NodeEvent
    from app.services import heartbeat_service as hb
    import time
    now = time.time()
    with app.app_context():
        assert hb.sync(data={'nodes': [
            {'name': 'pi-01', 'status': 'online', 'last_seen': now, 'since': now - 60, 'agent_version': '1.1.0'},
            {'name': 'unknown-node', 'status': 'online', 'last_seen': now}]}) == 1
        n = Node.query.filter_by(name='pi-01').first()
        assert n.hb_status == 'online' and n.hb_installed and n.hb_agent_version == '1.1.0' and hb.fresh(n)
        hb.sync(data={'nodes': [{'name': 'pi-01', 'status': 'offline', 'last_seen': now, 'lwt_at': now}]})
        n = Node.query.filter_by(name='pi-01').first()
        assert n.hb_status == 'offline' and n.hb_lwt_at and not hb.fresh(n)
        ev = NodeEvent.query.filter_by(kind='heartbeat_changed').all()
        assert [(e.old_value, e.new_value) for e in ev] == [('online', 'offline')]


def test_a_stale_heartbeat_does_not_count(app):
    from app.services import heartbeat_service as hb
    with app.app_context():
        n = Node.query.filter_by(name='pi-01').first()
        n.hb_status, n.hb_last_seen = 'online', datetime.utcnow() - timedelta(seconds=120)
        assert not hb.fresh(n)


def test_the_dashboard_sla_counts_ssh_or_a_fresh_heartbeat(app):
    with app.app_context():
        a, b, c = Node.query.order_by(Node.name).all()
        a.status = 'online'
        b.status, b.hb_status, b.hb_last_seen = 'offline', 'online', datetime.utcnow()   # VPN gap
        c.status = 'offline'
        db.session.commit()
    d = login(app).get('/monitoring/fleet-summary').get_json()
    assert d['sla_percent'] == round(2 / 3 * 100, 1) and d['heartbeat_only'] == 1


def test_hourly_uptime_counts_heartbeat_minutes_as_up(app):
    from app.models import MetricsHourly, SystemMetrics
    from app.services import history
    hour = datetime.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    with app.app_context():
        nid = Node.query.filter_by(name='pi-00').first().id
        for m in range(60):
            ssh = m < 30
            db.session.add(SystemMetrics(node_id=nid, timestamp=hour + timedelta(minutes=m),
                                         status='online' if ssh else 'offline', heartbeat=(m < 50)))
        db.session.commit()
        history._rollup_hour(hour)
        db.session.commit()
        row = MetricsHourly.query.filter_by(node_id=nid, hour=hour).first()
        assert (row.samples, row.online, row.heartbeat_only, row.offline) == (60, 50, 20, 10)


def test_only_administrators_install_or_push_heartbeat_settings(app, monkeypatch):
    op = login(app, 'op', 'Operator-pass-1')
    for method, url, body in (('post', '/nodes/capabilities/install', {'capability': 'heartbeat'}),
                              ('post', '/nodes/heartbeat/settings', {'broker': 'x.example'}),
                              ('get', '/nodes/heartbeat/settings', None)):
        r = getattr(op, method)(url, json=body)
        assert r.status_code in (302, 403), url
    from app.routes import nodes as routes
    started = []
    monkeypatch.setattr(routes._jobs, 'run_each', lambda kind, title, owner, ids, work, **kw: started.append((kind, ids)) or 'j1')
    adm = login(app)
    r = adm.post('/nodes/capabilities/install', json={'capability': 'heartbeat'}).get_json()
    assert r['job_id'] == 'j1' and started[0][0] == 'heartbeat-install' and len(started[0][1]) == 3


def test_settings_are_saved_and_pushed_only_to_nodes_with_the_agent(app, monkeypatch):
    from app.routes import nodes as routes
    from app.services import heartbeat_service as hb
    with app.app_context():
        Node.query.filter_by(name='pi-02').first().hb_installed = True
        db.session.commit()
    started = []
    monkeypatch.setattr(routes._jobs, 'run_each', lambda kind, title, owner, ids, work, **kw: started.append((kind, ids)) or 'j2')
    adm = login(app)
    r = adm.post('/nodes/heartbeat/settings', json={'broker': 'mqtt.example.org', 'port': 8883,
                                                     'tls': True, 'interval': 20}).get_json()
    assert r['job_id'] == 'j2' and started[0][0] == 'heartbeat-config' and len(started[0][1]) == 1
    with app.app_context():
        assert hb.agent_settings()['broker'] == 'mqtt.example.org' and hb.agent_settings()['interval'] == 20
    bad = adm.post('/nodes/heartbeat/settings', json={'broker': 'bad host'})
    assert bad.status_code == 400


def test_without_a_master_key_nothing_can_be_installed(app):
    app.config['HEARTBEAT_MASTER_KEY'] = ''
    r = login(app).post('/nodes/capabilities/install', json={'capability': 'heartbeat'})
    assert r.status_code == 400 and 'HEARTBEAT_MASTER_KEY' in r.get_json()['message']


def test_the_install_sends_only_what_the_node_needs(app, monkeypatch):
    from app.services import collector, heartbeat_service as hb
    sent, ran = {}, []
    monkeypatch.setattr(hb, '_upload', lambda target, files: sent.update(files) or '/tmp/edgeshell-hb.abcd1234')
    monkeypatch.setattr(collector, 'run_command', lambda target, cmd, **kw: ran.append((cmd, kw)) or
                        {'success': True, 'stdout': 'Ways to the broker\nBroker: broker.hivemq.com over ws-tls on port 8884, '
                                                    'login accepted\nHEARTBEAT_OK 1.2.0\nDone.'})
    with app.app_context():
        n = Node.query.filter_by(name='pi-01').first()
        ok, out = hb.install(n)
        assert ok and n.hb_installed and n.hb_way == 'ws-tls:8884'
    assert set(sent) == {'heartbeat_agent.py', 'heartbeat_protocol.py', 'install.sh', 'setup.py', 'token', 'login'}
    assert MASTER not in ''.join(sent.values())
    cmd, kw = ran[0]
    assert 'sudo python3 /tmp/edgeshell-hb.abcd1234/setup.py --token-file' in cmd and '--login-file' in cmd
    assert 'rm -rf /tmp/edgeshell-hb.abcd1234' in cmd and 'admin123' not in cmd     # secrets only in files
    assert '\r' not in sent['install.sh'] and '\r' not in sent['setup.py']          # Unix line endings reach the node


def test_the_join_token_is_what_the_agents_setup_py_reads(app):
    import importlib.util
    from app.services import heartbeat_service as hb
    spec = importlib.util.spec_from_file_location('vendored_setup', os.path.join(hb.AGENT_DIR, 'setup.py'))
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    with app.app_context():
        n = Node.query.filter_by(name='pi-01').first()
        token = hb.join_token(n)
        t = setup.read_token(token)
        assert t['node'] == 'pi-01' and MASTER not in token
        assert dict(line.split('=', 1) for line in setup.agent_env(t).splitlines()) == \
            hb.protocol().provision(bytes.fromhex(MASTER), 'pi-01')


def test_the_vendored_agent_is_one_release(app):
    """heartbeat_agent.py, setup.py and install.sh from the same agent release."""
    from app.services import heartbeat_service as hb
    text = open(os.path.join(hb.AGENT_DIR, 'setup.py'), encoding='utf-8').read()
    assert '--merge-config' in text and '--set-credentials' in text and hb.agent_version() == '1.2.0'


def _node(app, **kw):
    n = Node.query.filter_by(name='pi-01').first()
    for k, v in kw.items():
        setattr(n, k, v)
    db.session.commit()
    return n


def test_the_routine_plans_install_update_repair_and_leaves_working_nodes_alone(app):
    from app.services import heartbeat_service as hb
    now = datetime.utcnow()
    with app.app_context():
        n = _node(app, hb_installed=None)
        assert hb.plan(n, now) == 'install'
        n = _node(app, hb_installed=True, hb_agent_version='1.1.0', hb_status='online', hb_last_seen=now)
        assert hb.plan(n, now) == 'update'                                   # older than the shipped 1.2.0
        n = _node(app, hb_agent_version='1.2.0')
        assert hb.plan(n, now) is None                                       # current and fresh: nothing to do
        n = _node(app, hb_status='silent', hb_last_seen=now - timedelta(minutes=5))
        assert hb.plan(n, now) is None                                       # a short gap: wait
        n = _node(app, hb_last_seen=now - timedelta(minutes=20))
        assert hb.plan(n, now) == 'repair'                                   # SSH works, no heartbeat for long
        n = _node(app, hb_routine_at=now - timedelta(hours=2), hb_routine_note='failed repair: no way in')
        assert hb.plan(n, now) is None                                       # failed recently: leave it a day
        n = _node(app, hb_routine_at=now - timedelta(hours=2), hb_routine_note='repaired: Broker: x over tcp')
        assert hb.plan(n, now) is None                                       # worked on recently, any result: a pause
        n = _node(app, hb_routine_at=now - timedelta(hours=25))
        assert hb.plan(n, now) == 'repair'
        for kw in ({'status': 'offline'}, {'is_deleted': True}, {'login_paused_at': now}):
            n = _node(app, **dict({'status': 'online', 'is_deleted': False, 'login_paused_at': None}, **kw))
            assert hb.plan(n, now) is None                                   # only nodes SSH reaches now


def test_a_repair_tests_first_and_reinstalls_only_when_the_test_fails(app, monkeypatch):
    from app.services import heartbeat_service as hb
    calls = []
    monkeypatch.setattr(hb, 'test', lambda node: calls.append('test') or (True, 'HEARTBEAT_TEST ok'))
    monkeypatch.setattr(hb, 'install', lambda node: calls.append('install') or (True, 'Broker: x over tcp on port 1883\nDone.'))
    with app.app_context():
        n = _node(app, hb_installed=True)
        ok, note = hb.run_routine_node(n, 'repair')
        assert ok and calls == ['test'] and n.hb_routine_note.startswith('checked')
        monkeypatch.setattr(hb, 'test', lambda node: calls.append('test') or (False, 'FAIL  service'))
        ok, note = hb.run_routine_node(n, 'repair')
        assert ok and calls[-2:] == ['test', 'install'] and note.startswith('repaired')
        monkeypatch.setattr(hb, 'install', lambda node: (False, 'Not set up: no way to the broker works'))
        ok, note = hb.run_routine_node(n, 'repair')
        assert not ok and n.hb_routine_note.startswith('failed repair: Not set up: no way')


def test_the_routine_runs_as_a_job_and_records_its_pass(app, monkeypatch):
    from app.services import heartbeat_service as hb, jobs
    seen = []
    monkeypatch.setattr(hb, 'sync', lambda *a, **k: 3)
    monkeypatch.setattr(hb, 'run_routine_node', lambda node, action: seen.append((node.name, action)) or (True, 'installed'))

    def run_each(kind, title, owner, items, work, workers=16, summary=None):
        job = {'ok': 0, 'failed': 0}
        for it in items:
            job['ok' if work(it) else 'failed'] += 1
        summary(job)
        return 'job1'
    monkeypatch.setattr(jobs, 'run_each', run_each)
    with app.app_context():
        for n in Node.query.all():
            n.hb_installed = None
        db.session.commit()
    c = login(app)
    assert login(app, 'op', 'Operator-pass-1').post('/nodes/heartbeat/routine', json={'run_now': True}).status_code == 403
    r = c.post('/nodes/heartbeat/routine', json={'enabled': True, 'hours': 4, 'run_now': True})
    d = r.get_json()
    assert d['success'] and d['job_id'] == 'job1' and sorted(seen) == [('pi-00', 'install'), ('pi-01', 'install'),
                                                                      ('pi-02', 'install')]
    g = c.get('/nodes/heartbeat/settings').get_json()
    assert g['routine'] == {'enabled': True, 'hours': 4} and g['routine_last']['planned']['install'] == 3
    assert g['routine_last']['result'] == '3 ok, 0 failed' and g['broker_user'] == 'admin'
    assert c.post('/nodes/heartbeat/routine', json={'hours': 0}).status_code == 400


def test_the_routine_changes_nothing_when_sensehive_is_not_reachable(app, monkeypatch):
    from app.services import heartbeat_service as hb, jobs
    monkeypatch.setattr(hb, 'sync', lambda *a, **k: None)              # SenseHive down: heartbeat state is stale
    monkeypatch.setattr(hb, 'sensehive_configured', lambda: True)
    started = []
    monkeypatch.setattr(jobs, 'run_each', lambda *a, **k: started.append(1) or 'j')
    with app.app_context():
        for n in Node.query.all():
            n.hb_installed, n.hb_agent_version, n.hb_status = True, '1.2.0', 'silent'
        db.session.commit()
        jid, summary = hb.run_routine()
        assert jid is None and started == [] and summary['result'].startswith('skipped: SenseHive was not reachable')
        assert hb.routine_last()['result'].startswith('skipped')


def test_the_routine_respects_its_limit(app, monkeypatch):
    from app.services import heartbeat_service as hb, jobs
    monkeypatch.setattr(hb, 'sync', lambda *a, **k: 3)
    monkeypatch.setattr(jobs, 'run_each', lambda kind, title, owner, items, work, **kw: (seen.extend(items), 'j')[1])
    seen = []
    app.config['HEARTBEAT_ROUTINE_MAX'] = 2
    with app.app_context():
        jid, summary = hb.run_routine()
    assert len(seen) == 2 and summary['deferred'] == 1


def test_the_keeper_waits_then_runs_every_n_hours(app, monkeypatch):
    from app.services import heartbeat_service as hb
    runs = []
    monkeypatch.setattr(hb, 'run_routine', lambda *a, **k: runs.append(1) or (None, {}))
    clock = [1_000_000.0]
    monkeypatch.setattr(hb.time, 'time', lambda: clock[0])
    with app.app_context():
        k = hb.Keeper()
        k.tick()
        assert runs == []                              # not right after a start
        clock[0] += hb.Keeper.FIRST_AFTER_S
        k.tick()
        assert runs == [1]
        clock[0] += 3600
        k.tick()
        assert runs == [1]                             # every 6 hours by default
        clock[0] += 5 * 3600
        k.tick()
        assert runs == [1, 1]
        hb.save_routine_settings(False, 6)
        clock[0] += 7 * 3600
        k.tick()
        assert runs == [1, 1]                          # off means off


def test_a_settings_push_merges_on_nodes_with_a_new_agent(app, monkeypatch):
    from app.services import collector, heartbeat_service as hb
    ran = []
    monkeypatch.setattr(hb, '_upload', lambda target, files: '/tmp/edgeshell-hb.abcd1234')
    monkeypatch.setattr(collector, 'run_command', lambda target, cmd, **kw: ran.append(cmd) or
                        {'success': True, 'stdout': 'CONFIG_PUSHED broker x'})
    with app.app_context():
        ok, msg = hb.push_config(Node.query.filter_by(name='pi-01').first())
    assert ok and '--merge-config' in ran[0]


def test_the_broker_login_push_is_for_administrators_and_runs_as_a_job(app, monkeypatch):
    from app.services import heartbeat_service as hb, jobs
    monkeypatch.setattr(jobs, 'run_each', lambda kind, title, owner, items, work, **kw: 'jl')
    with app.app_context():
        for n in Node.query.all():
            n.hb_installed = True
        db.session.commit()
    assert login(app, 'op', 'Operator-pass-1').post('/nodes/heartbeat/login', json={}).status_code == 403
    assert login(app).post('/nodes/heartbeat/login', json={}).get_json()['job_id'] == 'jl'
    with app.app_context():
        assert hb.login_env() == 'HB_MQTT_USERNAME=admin\nHB_MQTT_PASSWORD=admin123\n'


@pytest.mark.parametrize('who,code', [(None, 401), ('op', 403), ('admin', 401)])   # admin: needs a pass first
def test_nginx_admin_check_for_sensehive(app, who, code):
    c = app.test_client() if who is None else (login(app) if who == 'admin' else login(app, 'op', 'Operator-pass-1'))
    assert c.get('/internal/admin-check').status_code == code


def test_the_sensehive_link_is_for_administrators_only(app):
    assert 'action="/tools/sensehive/launch"' in login(app).get('/nodes/').get_data(as_text=True)
    assert '/tools/sensehive/launch' not in login(app, 'op', 'Operator-pass-1').get('/nodes/').get_data(as_text=True)


@pytest.mark.parametrize('status,installed,text', [
    ('online', True, 'heartbeat'), ('offline', True, 'LWT offline'), ('silent', True, 'heartbeat silent'),
    (None, False, 'no heartbeat agent')])
def test_every_role_sees_the_heartbeat_on_the_card(app, status, installed, text):
    with app.app_context():
        n = Node.query.filter_by(name='pi-01').first()
        n.hb_status, n.hb_installed, n.hb_last_seen = status, installed, datetime.utcnow()
        db.session.commit()
    page = login(app, 'op', 'Operator-pass-1').get('/nodes/').get_data(as_text=True)
    assert text in page


def test_the_capability_scan_reads_the_agent_state(app, monkeypatch):
    from app.services import capability_service as cap
    out = 'ARCH:aarch64\nDOCKER:1\nNODERED:0\nORGUI:0\nHEARTBEAT:1\n'
    monkeypatch.setattr(cap, 'execute_command', lambda node, cmd, timeout=20: {'success': True, 'stdout': out})
    with app.app_context():
        summary = cap.scan_fleet(Node.query.all())
        assert summary['counts']['heartbeat']['installed'] == 3
        assert all(n.hb_installed for n in Node.query.all())
        assert 'HB_RUNNING' in cap._check_cmd(6080)


def test_a_test_group_gets_the_agent_and_nobody_else(app, monkeypatch):
    from app.routes import nodes as routes
    started = []
    monkeypatch.setattr(routes._jobs, 'run_each', lambda kind, title, owner, ids, work, **kw: started.append(ids) or 'j3')
    with app.app_context():
        picked = [n.id for n in Node.query.order_by(Node.name).limit(2)]
    adm = login(app)
    r = adm.post('/nodes/capabilities/install', json={'capability': 'heartbeat', 'node_ids': picked}).get_json()
    assert r['job_id'] == 'j3' and sorted(started[0]) == sorted(picked)
    page = adm.get('/nodes/').get_data(as_text=True)
    assert 'node_ids: picked' in page                      # Fleet Setup sends the ticked nodes


@pytest.mark.parametrize('cap', ['orgui', 'docker', 'nodered', 'heartbeat'])
def test_install_on_selected_nodes_runs_as_a_job_for_every_kind(app, monkeypatch, cap):
    from app.routes import nodes as routes
    started = []
    monkeypatch.setattr(routes._jobs, 'run_each', lambda kind, title, owner, ids, work, **kw: started.append((kind, ids)) or 'j9')
    with app.app_context():
        ids = [n.id for n in Node.query.order_by(Node.name).limit(2)]
    r = login(app).post('/nodes/capabilities/install', json={'capability': cap, 'node_ids': ids}).get_json()
    assert r['job_id'] == 'j9' and sorted(started[0][1]) == sorted(ids) and cap in started[0][0]


def test_the_bulk_bar_offers_install_to_administrators_only(app):
    page = login(app).get('/nodes/').get_data(as_text=True)
    assert 'id="bulkInstallSel"' in page and 'Install on selected' in page and 'id="hbRefreshBtn"' in page
    op = login(app, 'op', 'Operator-pass-1').get('/nodes/').get_data(as_text=True)
    assert 'id="bulkInstallSel"' not in op and 'id="hbRefreshBtn"' in op       # operators may refresh, not install


def test_heartbeat_refresh_counts_from_sensehive_without_ssh(app, monkeypatch):
    import time
    from app.services import heartbeat_service as hb
    now = time.time()
    monkeypatch.setattr(hb, 'fetch_nodes', lambda: {'nodes': [
        {'name': 'pi-00', 'status': 'online', 'last_seen': now}, {'name': 'pi-01', 'status': 'offline', 'last_seen': now}]})
    d = login(app, 'op', 'Operator-pass-1').post('/nodes/heartbeat/refresh').get_json()
    assert d['success'] and d['counts'] == {'online': 1, 'offline': 1, 'silent': 0, 'no_agent': 1} and d['total'] == 3


def test_fleet_setup_opens_from_stored_results_without_ssh(app, monkeypatch):
    from app.services import capability_service as cap
    monkeypatch.setattr(cap, 'execute_command', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no SSH on open')))
    with app.app_context():
        n = Node.query.filter_by(name='pi-00').first()
        n.docker_installed, n.hb_status = True, 'online'
        db.session.commit()
    d = login(app).get('/nodes/capabilities/summary').get_json()
    assert d['total'] == 3 and d['counts']['docker']['installed'] == 1 and d['counts']['heartbeat']['installed'] == 1


def test_a_single_capability_check_is_a_background_job(app, monkeypatch):
    from app.routes import nodes as routes
    from app.services import capability_service as cap
    started = []
    monkeypatch.setattr(routes._jobs, 'run_each', lambda kind, title, owner, ids, work, **kw: started.append((title, work, ids)) or 'jc')
    r = login(app).post('/nodes/capabilities/scan', json={'capabilities': ['orgui']}).get_json()
    assert r['job_id'] == 'jc' and r['capabilities'] == ['orgui'] and 'OpenRemoteGUI' in started[0][0]
    cmds = []
    monkeypatch.setattr(cap, 'execute_command', lambda node, cmd, timeout=20: cmds.append(cmd) or
                        {'success': True, 'stdout': 'ARCH:aarch64\nORGUI:1\n'})
    with app.app_context():
        assert started[0][1](started[0][2][0])                 # the job's work: probe and store
        assert Node.query.get(started[0][2][0]).orgui_installed is True
    assert 'ORGUI:' in cmds[0] and 'DOCKER:' not in cmds[0]   # only what was asked
