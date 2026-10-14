"""Node login policy: no repeated logins with a rejected password, automatic
recovery without guessing, and every SSH login through the one guarded helper."""
import pathlib
import re

import pytest

from app import create_app, db
from app.models.node import Node
from app.models.user import User

ROOT = pathlib.Path(__file__).resolve().parent.parent


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
        for name in ('pi-ok', 'pi-badpw', 'pi-ts'):
            n = Node(name=name, ip_address='10.0.0.1', username='pi', status='online')
            n.password = 'pw'
            db.session.add(n)
        db.session.commit()
    yield app


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    return c


@pytest.fixture
def fleet(app, monkeypatch):
    """Fake nodes: pi-ok logs in, pi-badpw rejects the password, pi-ts is
    Tailscale SSH in check mode. Records every login attempt."""
    import app.services.collector as col
    from app.services import login_policy
    attempts = []
    state = {'ts_fixed': False, 'banners': {}}
    with app.app_context():
        names = {n.id: n.name for n in Node.query.all()}

    def collect_one(t):
        name = names[t['id']]
        attempts.append(name)
        if name == 'pi-ok' or (name == 'pi-ts' and state['ts_fixed']):
            return {'ok': True, 'metrics': _metrics(), 'kind': None, 'message': None, 'seconds': 0.1}
        reason = 'tailscale' if name == 'pi-ts' else 'password'
        return {'ok': False, 'metrics': None, 'kind': 'auth', 'reason': reason,
                'banner': 'SSH-2.0-Tailscale' if reason == 'tailscale' else 'SSH-2.0-OpenSSH_9.2',
                'message': 'Authentication failed', 'seconds': 0.1}
    monkeypatch.setattr(col, 'collect_one', collect_one)
    monkeypatch.setattr(login_policy, 'read_banner',
                        lambda ip, port, timeout=5: state['banners'].get('all'))
    c = col.Collector()
    return c, attempts, state, names


def _metrics():
    return {'cpu_percent': 1, 'mem_percent': 2, 'disk_percent': 3, 'temperature': 40, 'load_1': 0.1,
            'load_5': 0.1, 'load_15': 0.1, 'uptime': '1 day', 'uptime_seconds': 100, 'arch': 'aarch64',
            'docker_installed': False, 'nodered_installed': False, 'orgui_running': False}


def _node(name):
    return Node.query.filter_by(name=name).first()


def test_rejected_password_pauses_after_the_limit_and_is_not_retried(app, fleet):
    c, attempts, _, _ = fleet
    with app.app_context():
        app.config['LOGIN_PAUSE_AFTER_FAILURES'] = 2
        c.run_cycle(force=True)
        assert _node('pi-badpw').login_paused_at is None              # one failure: not yet
        assert _node('pi-ts').login_pause_reason == 'tailscale'        # Tailscale check: at once
        c.run_cycle(force=True)
        bad = _node('pi-badpw')
        assert bad.login_paused_at is not None and bad.login_pause_reason == 'password'
        assert 'paused' in bad.last_error
        attempts.clear()
        for _ in range(5):                                             # bulk and forced runs
            c.run_cycle(force=True)
            c.run_cycle(node_ids=[bad.id], force=True)
        assert 'pi-badpw' not in attempts and 'pi-ts' not in attempts and 'pi-ok' in attempts
        from app.models import ActivityLog
        assert ActivityLog.query.filter_by(action='Logins paused').count() == 2


def test_pause_survives_a_restart(app, fleet):
    import app.services.collector as col
    c, attempts, _, _ = fleet
    with app.app_context():
        c.run_cycle(force=True)
        c.run_cycle(force=True)
        attempts.clear()
        col.Collector().run_cycle(force=True)                          # a fresh process
        assert attempts == ['pi-ok']


def test_one_manual_attempt_is_allowed_and_success_resumes(app, client, fleet, monkeypatch):
    c, attempts, state, names = fleet
    import app.services.collector as col
    monkeypatch.setattr(col, 'COLLECTOR', c)
    with app.app_context():
        c.run_cycle(force=True)
        ts = _node('pi-ts').id
    state['ts_fixed'] = True
    attempts.clear()
    r = client.post(f'/nodes/{ts}/test', json={'bulk': 1}).get_json()
    assert r['paused'] and attempts == []                              # Test All skips it
    r = client.post(f'/nodes/{ts}/test', json={}).get_json()
    assert r['success']
    with app.app_context():
        assert _node('pi-ts').login_paused_at is None


def test_recheck_without_password_classifies_and_canary_resumes_tailscale_group(app, fleet):
    from app.services import login_policy
    c, attempts, state, _ = fleet
    with app.app_context():
        for name in ('pi-badpw', 'pi-ts'):                             # paused by the upgrade fix
            n = _node(name)
            n.status, n.login_paused_at, n.login_pause_reason = 'auth_failed', db.func.now(), 'unknown'
        db.session.commit()
        state['banners']['all'] = 'SSH-2.0-Tailscale'
        attempts.clear()
        login_policy.recheck(c, force=True)
        assert _node('pi-ts').login_pause_reason == 'tailscale'
        assert len(attempts) == 1                                      # a single canary login
        state['ts_fixed'] = True
        login_policy.recheck(c, force=True)
        assert _node('pi-ts').login_paused_at is None and _node('pi-badpw').login_paused_at is None


def test_changed_ssh_server_gets_one_new_attempt(app, fleet):
    from app.services import login_policy
    c, attempts, state, _ = fleet
    with app.app_context():
        c.run_cycle(force=True)
        c.run_cycle(force=True)
        assert _node('pi-badpw').ssh_banner == 'SSH-2.0-OpenSSH_9.2'
        state['banners']['all'] = 'SSH-2.0-OpenSSH_9.2'
        login_policy.recheck(c, force=True)
        assert _node('pi-badpw').login_paused_at is not None          # same server: stays paused
        state['banners']['all'] = 'SSH-2.0-OpenSSH_9.6'
        login_policy.recheck(c, force=True)
        assert _node('pi-badpw').login_paused_at is None


def test_fix_login_and_retry_resume(app, client, fleet, monkeypatch):
    c, _, _, _ = fleet
    import app.services.collector as col
    monkeypatch.setattr(col, 'COLLECTOR', c)
    with app.app_context():
        c.run_cycle(force=True)
        c.run_cycle(force=True)
        bad = _node('pi-badpw').id
    r = client.post('/nodes/logins/resume', json={}).get_json()
    assert r['resumed'] == 2
    with app.app_context():
        assert Node.query.filter(Node.login_paused_at.isnot(None)).count() == 0
        c.run_cycle(force=True)                                        # fails again: paused again at once
        assert _node('pi-badpw').login_paused_at is not None
    r = client.post(f'/nodes/{bad}/credentials', json={'username': 'pi', 'password': 'new', 'ssh_port': 22})
    assert r.status_code == 200


def test_paused_nodes_are_refused_by_every_ssh_path(app):
    from app.services import collector as col, ssh_service
    with app.app_context():
        n = _node('pi-badpw')
        n.login_paused_at = db.func.now()
        db.session.commit()
        n = _node('pi-badpw')
        with pytest.raises(ssh_service.LoginPaused):
            ssh_service.get_ssh_client(n)
        r = col.run_command(col.target_for(n), 'uptime')
        assert not r['success'] and 'paused' in r['error']


def test_every_ssh_login_goes_through_the_guarded_helper():
    """Policy as code: a new feature that opens SSH by itself would bypass the
    login policy and the leak fix. Only ssh_service may create clients."""
    offenders = []
    for path in (ROOT / 'app').rglob('*.py'):
        rel = path.relative_to(ROOT).as_posix()
        if rel == 'app/services/ssh_service.py':
            continue
        text = path.read_text(encoding='utf-8')
        if re.search(r'SSHClient\(|\.connect\(\s*(hostname=|[a-z_\.\[\]\'"]+,\s*port=)', text):
            offenders.append(rel)
    assert not offenders, f'open SSH through app.services.ssh_service.connect: {offenders}'
