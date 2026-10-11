"""Production features: single session, timezone display, service events,
heartbeat access. Run on SQLite by default, or MariaDB with
TEST_DATABASE_URL=mysql+mysqlconnector://... pytest"""
import pytest
from datetime import datetime
from app import create_app, db
from app.models.user import User


@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        db.drop_all()
        db.create_all()
        from app import _seed_defaults
        _seed_defaults()
        u = User.query.filter_by(username='admin').first()
        u.must_change_password = False
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()


def _post_login(c):
    return c.post('/login', data={'username': 'admin', 'password': 'admin123'})


def test_iso_utc_has_z():
    from app.utils.timeutil import iso_utc
    assert iso_utc(datetime(2026, 9, 30, 11, 37, 22, 914307)) == '2026-09-30T11:37:22Z'
    assert iso_utc(None) is None


def test_localtime_filter_ist(app):
    with app.test_request_context():
        f = app.jinja_env.filters['localtime']
        assert f(datetime(2026, 9, 30, 11, 37, 22), '%H:%M') == '17:07'


def test_single_session_takeover(app):
    a, b = app.test_client(), app.test_client()
    assert _post_login(a).status_code == 302
    assert a.get('/').status_code == 200

    # Second device: password OK, but must confirm before taking over.
    r = _post_login(b)
    assert r.status_code == 200 and b'already signed in' in r.data
    assert a.get('/').status_code == 200          # A still alive until confirm

    r = b.post('/login/confirm')
    assert r.status_code == 302
    assert b.get('/').status_code == 200

    # A is now revoked: its background poll must see it (401 session_revoked)
    r = a.get('/session/status', headers={'Accept': 'application/json'})
    assert r.status_code == 401 and r.get_json()['error'] == 'session_revoked'
    assert 'another device' in r.get_json()['message']
    _post_login(a); a.post('/login/confirm')          # A takes it back
    r = b.get('/', headers={'Accept': 'application/json'})
    assert r.status_code == 401 and r.get_json()['error'] == 'session_revoked'
    assert b.get('/').status_code == 302


def test_agent_label(app):
    from app.services.session_service import _agent_label
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36'
    with app.test_request_context(headers={'User-Agent': ua}):
        assert _agent_label() == 'Chrome on Windows'
    ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Mobile Safari/604.1'
    with app.test_request_context(headers={'User-Agent': ua}):
        assert _agent_label() == 'Safari on iPhone'


def test_logout_frees_account(app):
    a, b = app.test_client(), app.test_client()
    _post_login(a)
    a.get('/logout')
    assert _post_login(b).status_code == 302      # no takeover prompt


def test_cancel_keeps_other_session(app):
    a, b = app.test_client(), app.test_client()
    _post_login(a)
    _post_login(b)
    b.get('/login/cancel')
    assert b.post('/login/confirm').status_code == 302  # pending gone -> expired
    assert b.get('/').status_code == 302                # b not signed in
    assert a.get('/').status_code == 200                # a untouched


def _set(app, **fields):
    with app.app_context():
        u = User.query.filter_by(username='admin').first()
        for k, v in fields.items():
            setattr(u, k, v)
        db.session.commit()


def test_session_limit_allows_n_devices(app):
    _set(app, max_sessions=2)
    a, b, c = app.test_client(), app.test_client(), app.test_client()
    assert _post_login(a).status_code == 302
    assert _post_login(b).status_code == 302          # 2nd device, no prompt
    assert a.get('/').status_code == 200 and b.get('/').status_code == 200
    r = _post_login(c)                                  # 3rd: over the limit
    assert r.status_code == 200 and b'already signed in' in r.data
    c.post('/login/confirm')
    assert c.get('/').status_code == 200
    # The least recently active device (a) is the one signed out.
    r = a.get('/session/status', headers={'Accept': 'application/json'})
    assert r.status_code == 401 and 'another device' in r.get_json()['message']
    assert b.get('/').status_code == 200


def test_session_limit_clamped_and_default(app):
    from app.models.user import clamp_sessions
    assert clamp_sessions(0) == 1 and clamp_sessions(9) == 4 and clamp_sessions('x') == 1
    with app.app_context():
        assert User.query.filter_by(username='admin').first().max_sessions == 1


def test_lowering_limit_signs_out_oldest(app):
    _set(app, max_sessions=3)
    a, b = app.test_client(), app.test_client()
    _post_login(a); _post_login(b)
    b.post('/users/1/sessions', data={'max_sessions': '1'})
    assert a.get('/', headers={'Accept': 'application/json'}).status_code == 401
    assert b.get('/').status_code == 200


def test_password_reset_and_disable_revoke_sessions(app):
    with app.app_context():
        v = User(username='victim', email='v@x', role='operator',
                 is_active=True, must_change_password=False, max_sessions=1)
        v.set_password('pw-12345678'); db.session.add(v); db.session.commit()
        vid = v.id
    admin, victim = app.test_client(), app.test_client()
    _post_login(admin)
    victim.post('/login', data={'username': 'victim', 'password': 'pw-12345678'})
    assert victim.get('/').status_code == 200

    admin.post(f'/users/{vid}/reset-password', data={'new_password': 'new-pass-123'})
    r = victim.get('/', headers={'Accept': 'application/json'})
    assert r.status_code == 401 and 'reset your password' in r.get_json()['message']

    victim.post('/login', data={'username': 'victim', 'password': 'new-pass-123'})
    assert victim.get('/').status_code == 200
    admin.post(f'/users/{vid}/toggle')                 # disable: signed out
    assert victim.get('/').status_code == 302
    admin.post(f'/users/{vid}/toggle')                 # re-enable: old cookie stays dead
    assert victim.get('/', headers={'Accept': 'application/json'}).status_code == 401


def test_logout_kills_copied_cookie(app):
    """A stolen session cookie must stop working once the owner logs out."""
    a = app.test_client()
    _post_login(a)
    with a.session_transaction() as s:
        stolen = dict(s)
    a.get('/logout')
    thief = app.test_client()
    with thief.session_transaction() as s:
        s.update(stolen)
    assert thief.get('/', headers={'Accept': 'application/json'}).status_code == 401


def test_login_does_not_reveal_usernames(app):
    c = app.test_client()
    unknown = c.post('/login', data={'username': 'nobody', 'password': 'x'}).data
    _set(app, locked_until=datetime(2999, 1, 1))
    locked = c.post('/login', data={'username': 'admin', 'password': 'admin123'}).data
    for body in (unknown, locked):
        assert b'temporarily locked' in body and b'Account locked.' not in body
    assert c.get('/').status_code == 302               # correct password refused while locked


def test_session_status(app):
    a = app.test_client()
    assert a.get('/session/status').get_json() == {'authenticated': False}
    _post_login(a)
    assert a.get('/session/status').get_json() == {'authenticated': True}


def test_heartbeat_loopback_only(app, monkeypatch):
    import app.routes.vpn as vpn
    monkeypatch.setattr(vpn, 'get_status', lambda: {'connected': True, 'self_ip': '100.1.1.1',
                                                   'node_count': 0, 'nodes': []})
    c = app.test_client()
    assert c.post('/vpn/heartbeat').status_code == 200
    r = c.post('/vpn/heartbeat', headers={'X-Forwarded-For': '8.8.8.8'})
    assert r.status_code == 401


def test_service_events(app, monkeypatch, tmp_path):
    from app.services import service_events as se
    from app.models import ServiceEvent, ActivityLog
    monkeypatch.setattr(se, 'MARKER', str(tmp_path / 'm.json'))
    monkeypatch.setattr(se, '_system_stopping', lambda: False)
    with app.app_context():
        assert se.record_start().reason == 'first_start'
        monkeypatch.setenv('SERVICE_RESULT', 'exit-code')
        monkeypatch.setenv('EXIT_CODE', 'exited')
        monkeypatch.setenv('EXIT_STATUS', '1')
        assert se.record_stop().reason == 'crash'
        assert se.record_start().reason == 'crash_restart'
        (tmp_path / 'm.json').write_text('{"reason": "watchdog", "detail": "3 failed checks"}')
        monkeypatch.setenv('SERVICE_RESULT', 'success')
        se.record_stop()
        assert se.record_start().reason == 'watchdog'
        monkeypatch.setattr(se, '_boot_id', lambda: 'another-boot')
        assert se.record_start().reason == 'host_boot'
        assert ServiceEvent.query.count() == 6
        assert ActivityLog.query.filter(ActivityLog.action.like('Service %')).count() == 6
