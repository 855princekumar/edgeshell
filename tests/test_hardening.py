"""Security hardening that the scanners asked for, and a shell injection they
missed: paths sent to nodes are validated and quoted, errors shown to people
never carry exception text, redirects stay on this site, and HTML stripping
stays fast on hostile input."""
import shlex
import time

import pytest

from app import create_app, db, limiter
from app.models.node import Node
from app.models.user import User


@pytest.fixture
def app():
    app = create_app('testing')
    limiter.reset()
    with app.app_context():
        db.drop_all()
        db.create_all()
        from app import _seed_defaults
        _seed_defaults()
        a = User.query.filter_by(username='admin').first()
        a.must_change_password, a.email = False, 'owner@fleet.test'
        u = User(username='mon', email='mon@fleet.test', role='monitor', is_active=True, must_change_password=False)
        u.set_password('Monitor-pass-1')
        db.session.add(u)
        db.session.add(Node(name='n1', ip_address='10.0.0.1', username='pi', status='online'))
        db.session.commit()
    yield app


@pytest.fixture
def ran(monkeypatch):
    """Every command sent to a node, instead of sending it."""
    sent = []
    import app.routes.logs as logs

    def fake(node, cmd, timeout=60):
        sent.append(cmd)
        return {'success': True, 'stdout': '', 'stderr': '', 'exit_code': 0}
    monkeypatch.setattr(logs, 'execute_command', fake)
    return sent


def monitor(app):
    c = app.test_client()
    c.post('/login', data={'username': 'mon', 'password': 'Monitor-pass-1'})
    return c


EVIL = '/var/log; touch /tmp/pwned #'


def test_a_read_only_user_cannot_add_commands_through_a_log_path(app, ran):
    c = monitor(app)
    with app.app_context():
        nid = Node.query.first().id
    c.post('/logs/browse', json={'node_id': nid, 'log_path': EVIL})
    c.post('/logs/fetch', json={'node_id': nid, 'file_path': EVIL, 'lines': 50})
    assert len(ran) == 2
    for cmd in ran:
        assert shlex.quote(EVIL) in cmd                      # one quoted argument
        assert '; touch' not in cmd.replace(shlex.quote(EVIL), '')


@pytest.mark.parametrize('bad', ['relative/path', '/var/log\nreboot', '/x\x00y', '/' + 'a' * 2000, 123])
def test_paths_that_are_not_plain_absolute_paths_are_refused(app, ran, bad):
    c = monitor(app)
    with app.app_context():
        nid = Node.query.first().id
    assert c.post('/logs/browse', json={'node_id': nid, 'log_path': bad}).status_code == 400
    assert c.post('/logs/fetch', json={'node_id': nid, 'file_path': bad}).status_code == 400
    assert ran == []


def test_the_line_count_is_a_bounded_number(app, ran):
    c = monitor(app)
    with app.app_context():
        nid = Node.query.first().id
    c.post('/logs/fetch', json={'node_id': nid, 'file_path': '/var/log/syslog', 'lines': '5; reboot'})
    c.post('/logs/fetch', json={'node_id': nid, 'file_path': '/var/log/syslog', 'lines': 10 ** 9})
    assert 'tail -n 200 ' in ran[0] and 'tail -n 10000 ' in ran[1] and 'reboot' not in ' '.join(ran)


def test_errors_shown_to_people_never_carry_exception_text(app):
    import socket
    import paramiko
    from app.services.errors import describe
    secret = 'host=10.9.9.9 path=/srv/private/key.pem token=abc123'
    for exc in (RuntimeError(secret), OSError(secret), paramiko.SSHException(secret),
                socket.timeout(secret), PermissionError(secret), ValueError(secret)):
        msg = describe(exc, 'test', log_it=False)
        assert secret not in msg and '10.9.9.9' not in msg and 'key.pem' not in msg, (exc, msg)
    from app.services.collector import classify
    kind, msg = classify(paramiko.SSHException(secret))
    assert secret not in msg


def test_health_does_not_reveal_database_errors(app, monkeypatch):
    from app.routes import health as h
    def boom(*a, **k):
        raise RuntimeError('mysql://edgeshell:hunter2@db/edgeshell refused')
    monkeypatch.setattr(h.db.session, 'execute', boom)
    body = app.test_client().get('/healthz').get_data(as_text=True)
    assert 'hunter2' not in body and 'mysql://' not in body


@pytest.mark.parametrize('target,expected', [
    ('/nodes/', '/nodes/'), ('/users/api?x=1', '/users/api?x=1'),
    ('//evil.test/x', None), ('https://evil.test', None), ('/\\evil.test', '/evil.test'),
    ('\\\\evil.test', None), ('javascript:alert(1)', None), ('', None), (None, None)])
def test_sign_in_only_returns_to_this_site(target, expected):
    from app.routes.auth import _safe_next
    got = _safe_next(target)
    assert (got is None) == (expected is None)
    if expected:
        assert got == expected


def test_stripping_html_stays_fast_on_hostile_input():
    from app.services.email_service import _strip_html
    t = time.time()
    _strip_html('<' * 100000)
    assert time.time() - t < 1
    assert _strip_html('<p>Hi <b>there</b></p>') == 'Hi there'
