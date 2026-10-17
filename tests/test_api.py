"""Public API (/api/v1): authentication with password + 2FA, tokens and keys,
permissions, validation, injection and abuse resistance, data points, events,
writes, commands, and an OpenAPI document that covers every route."""
import base64
import hashlib
import hmac
import json
import struct
import time
from datetime import datetime, timedelta

import pytest

from app import create_app, db, limiter
from app.models.api import ApiToken, NodeEvent
from app.models.node import Node
from app.models.user import User


def totp(secret, step=None):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8), casefold=True)
    step = int(time.time() // 30) if step is None else step
    d = hmac.new(key, struct.pack('>Q', step), hashlib.sha1).digest()
    o = d[-1] & 15
    return str((struct.unpack('>I', d[o:o + 4])[0] & 0x7fffffff) % 1000000).zfill(6)


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
        for name, role in (('op', 'operator'), ('mon', 'monitor')):
            u = User(username=name, email=f'{name}@fleet.test', role=role, is_active=True, must_change_password=False)
            u.set_password('Operator-pass-1')
            db.session.add(u)
        for i, (st, t) in enumerate((('online', 'pi-node'), ('offline', 'gateway'), ('auth_failed', 'mini-pc'))):
            n = Node(name=f'node-{i}', ip_address=f'10.0.0.{i + 1}', username='pi', status=st, node_type=t,
                     cpu_percent=10 + i, ram_percent=20, disk_percent=30)
            n.password = 'node-secret-pw'
            db.session.add(n)
        db.session.commit()
    yield app


def login(client, user='admin', password=None):
    pw = password or ('admin123' if user == 'admin' else 'Operator-pass-1')
    r = client.post('/api/v1/auth/login', json={'username': user, 'password': pw})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['token']


def H(token):
    return {'Authorization': f'Bearer {token}'}


# ---------------------------------------------------------------- auth

def test_password_then_2fa_gives_a_token(app):
    from app.services import mfa_service
    app.config['MFA_REQUIRED'] = True
    c = app.test_client()
    with app.app_context():
        u = User.query.filter_by(username='op').first()
        uri, secret = mfa_service.begin_enrollment(u)
        db.session.commit()
        mfa_service.confirm_enrollment(u, totp(secret))
        db.session.commit()
    r = c.post('/api/v1/auth/login', json={'username': 'op', 'password': 'Operator-pass-1'})
    body = r.get_json()
    assert r.status_code == 200 and body['mfa_required'] and body['challenge'].startswith('esc_') and 'token' not in body
    r = c.post('/api/v1/auth/verify', json={'challenge': body['challenge'], 'code': '000000'})
    assert r.status_code == 401
    r = c.post('/api/v1/auth/verify', json={'challenge': body['challenge'],
                                            'code': totp(secret, int(time.time() // 30) + 1)})
    assert r.status_code == 200, r.get_json()
    tok = r.get_json()['token']
    assert tok.startswith('est_') and r.get_json()['scopes'] == ['control', 'read', 'report', 'write']
    # the challenge is single use
    assert c.post('/api/v1/auth/verify', json={'challenge': body['challenge'], 'code': totp(secret)}).status_code == 401
    me = c.get('/api/v1/auth/whoami', headers=H(tok)).get_json()
    assert me['username'] == 'op' and me['credential']['kind'] == 'session'


def test_a_challenge_allows_three_wrong_codes(app):
    from app.services import mfa_service
    app.config['MFA_REQUIRED'] = True
    c = app.test_client()
    with app.app_context():
        u = User.query.filter_by(username='op').first()
        _, secret = mfa_service.begin_enrollment(u)
        db.session.commit()
        mfa_service.confirm_enrollment(u, totp(secret))
        db.session.commit()
    ch = c.post('/api/v1/auth/login', json={'username': 'op', 'password': 'Operator-pass-1'}).get_json()['challenge']
    for _ in range(3):
        c.post('/api/v1/auth/verify', json={'challenge': ch, 'code': '111111'})
    assert c.post('/api/v1/auth/verify', json={'challenge': ch, 'code': totp(secret)}).status_code == 401


def test_wrong_passwords_lock_the_account_and_answers_do_not_reveal_users(app):
    c = app.test_client()
    unknown = c.post('/api/v1/auth/login', json={'username': 'nobody', 'password': 'x'})
    wrong = c.post('/api/v1/auth/login', json={'username': 'op', 'password': 'wrong'})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.get_json() == wrong.get_json()
    for _ in range(5):
        c.post('/api/v1/auth/login', json={'username': 'op', 'password': 'wrong'})
    r = c.post('/api/v1/auth/login', json={'username': 'op', 'password': 'Operator-pass-1'})
    assert r.status_code in (423, 429)          # locked (or the per-address limit was reached first)


def test_login_is_rate_limited_per_address(app):
    c = app.test_client()
    codes = [c.post('/api/v1/auth/login', json={'username': 'nobody', 'password': 'x'}).status_code for _ in range(12)]
    assert 429 in codes and codes[0] == 401
    r = c.post('/api/v1/auth/login', json={'username': 'nobody', 'password': 'x'})
    assert r.status_code == 429 and r.get_json()['error'] == 'rate_limited' and r.headers.get('Retry-After')


def test_bad_tokens_are_refused_and_never_fall_back_to_the_cookie(app):
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})          # a browser session too
    for bad in ('Bearer est_aaaa_bbbb', 'Bearer ' + 'x' * 300, 'Basic abc', 'Bearer est_' + "'; drop table users;--"):
        r = c.get('/api/v1/nodes', headers={'Authorization': bad})
        assert r.status_code == 401, bad
    assert c.get('/api/v1/nodes').status_code == 200                              # cookie alone may read


def test_logout_and_revocation_end_the_token(app):
    c = app.test_client()
    tok = login(c)
    assert c.get('/api/v1/nodes', headers=H(tok)).status_code == 200
    assert c.post('/api/v1/auth/logout', headers=H(tok)).status_code == 200
    assert c.get('/api/v1/nodes', headers=H(tok)).status_code == 401


def test_idle_sessions_expire(app):
    c = app.test_client()
    tok = login(c)
    c.get('/api/v1/nodes', headers=H(tok))
    with app.app_context():
        row = ApiToken.query.filter_by(kind='session').first()
        row.last_used_at = datetime.utcnow() - timedelta(minutes=61)
        db.session.commit()
    assert c.get('/api/v1/nodes', headers=H(tok)).status_code == 401


def test_password_reset_ends_api_sessions_and_keys(app):
    from app.services.session_service import revoke_sessions
    c = app.test_client()
    tok = login(c, 'op')
    key = c.post('/api/v1/keys', headers=H(tok), json={'name': 'etl', 'purpose': 'automated test integration', 'scopes': ['read'],
                                                       'password': 'Operator-pass-1', 'code': '000000'}).get_json()['key']
    with app.test_request_context():
        revoke_sessions(User.query.filter_by(username='op').first(), 'password_reset')
        db.session.commit()
    assert c.get('/api/v1/nodes', headers=H(tok)).status_code == 401
    assert c.get('/api/v1/nodes', headers=H(key)).status_code == 401


def test_tokens_are_stored_only_as_hashes(app):
    c = app.test_client()
    tok = login(c)
    with app.app_context():
        row = ApiToken.query.filter_by(kind='session').first()
        secret = tok.split('_', 2)[2]
        assert secret not in row.secret_hash and row.secret_hash == hashlib.sha256(secret.encode()).hexdigest()


# ---------------------------------------------------------- service keys

def test_keys_need_a_session_and_the_password_and_cannot_exceed_the_role(app):
    c = app.test_client()
    tok = login(c, 'mon')
    base = {'name': 'grafana', 'purpose': 'automated test integration', 'scopes': ['read'], 'password': 'Operator-pass-1', 'code': '000000'}
    assert c.post('/api/v1/keys', headers=H(tok), json=dict(base, password='wrong')).status_code == 401
    assert c.post('/api/v1/keys', headers=H(tok), json=dict(base, scopes=['write'])).status_code == 403
    r = c.post('/api/v1/keys', headers=H(tok), json=base)
    assert r.status_code == 201
    key = r.get_json()['key']
    assert key.startswith('esk_') and 'key' not in r.get_json()['details']
    assert c.post('/api/v1/keys', headers=H(key), json=base).status_code == 403     # keys cannot mint keys
    listed = c.get('/api/v1/keys', headers=H(tok)).get_json()['keys']
    assert len(listed) == 1 and key.split('_')[1] == listed[0]['prefix'] and 'secret_hash' not in listed[0]
    assert c.delete(f"/api/v1/keys/{listed[0]['id']}", headers=H(tok)).status_code == 200
    assert c.get('/api/v1/nodes', headers=H(key)).status_code == 401


def test_key_ip_allowlist(app):
    c = app.test_client()
    tok = login(c, 'op')
    key = c.post('/api/v1/keys', headers=H(tok), json={'name': 'x', 'purpose': 'automated test integration', 'scopes': ['read'], 'allowed_ips': '192.0.2.0/24',
                                                       'password': 'Operator-pass-1', 'code': '000000'}).get_json()['key']
    r = c.get('/api/v1/nodes', headers=H(key))
    assert r.status_code == 403 and r.get_json()['error'] == 'ip_not_allowed'
    r = c.get('/api/v1/nodes', headers=H(key), environ_base={'REMOTE_ADDR': '192.0.2.7'})
    assert r.status_code == 200
    bad = c.post('/api/v1/keys', headers=H(tok), json={'name': 'y', 'purpose': 'automated test integration', 'scopes': ['read'], 'allowed_ips': 'not-an-ip',
                                                       'password': 'Operator-pass-1', 'code': '000000'})
    assert bad.status_code == 400


def test_a_read_key_cannot_write(app):
    c = app.test_client()
    tok = login(c, 'op')
    key = c.post('/api/v1/keys', headers=H(tok), json={'name': 'ro', 'purpose': 'automated test integration', 'scopes': ['read'], 'password': 'Operator-pass-1',
                                                       'code': '000000'}).get_json()['key']
    with app.app_context():
        nid = Node.query.first().id
    assert c.patch(f'/api/v1/nodes/{nid}', headers=H(key), json={'location': 'x'}).status_code == 403
    assert c.patch(f'/api/v1/nodes/{nid}', headers=H(tok), json={'location': 'x'}).status_code == 200


# -------------------------------------------------------- permissions, CSRF

def test_monitors_read_but_do_not_change(app):
    c = app.test_client()
    tok = login(c, 'mon')
    assert c.get('/api/v1/nodes', headers=H(tok)).status_code == 200
    with app.app_context():
        nid = Node.query.first().id
    assert c.patch(f'/api/v1/nodes/{nid}', headers=H(tok), json={'location': 'x'}).status_code == 403
    assert c.post('/api/v1/nodes', headers=H(tok), json={'name': 'n9', 'ip_address': '10.9.9.9', 'username': 'pi',
                                                          'password': 'x'}).status_code == 403


def test_a_browser_cookie_cannot_change_anything_through_the_api(app):
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    with app.app_context():
        nid = Node.query.first().id
    r = c.patch(f'/api/v1/nodes/{nid}', json={'location': 'evil'})
    assert r.status_code == 403 and r.get_json()['error'] == 'bearer_required'
    assert c.delete(f'/api/v1/nodes/{nid}').status_code == 403


def test_unauthenticated_requests_get_json_401(app):
    c = app.test_client()
    for path in ('/api/v1/nodes', '/api/v1/metrics/latest', '/api/v1/events', '/api/v1/keys'):
        r = c.get(path)
        assert r.status_code == 401 and r.is_json, path
    assert c.get('/api/v1/health').status_code == 200                    # health needs no sign-in
    r = c.get('/api/v1/does-not-exist')
    assert r.status_code in (401, 404) and r.is_json


# --------------------------------------------------- validation, injection

INJECTIONS = ["' OR '1'='1", "1; DROP TABLE nodes;--", '" OR 1=1 --', "%' UNION SELECT password_hash FROM users --",
              '<script>alert(1)</script>', '../../etc/passwd', '${jndi:ldap://x}', '\x00', 'A' * 5000,
              "admin'--", '1 OR 1=1', '{{7*7}}']


def test_injection_payloads_in_every_query_parameter_are_refused_or_harmless(app):
    from app.routes.api import ROUTES
    app.config['API_RATE_LIMIT'] = '100000 per minute'      # many requests on purpose
    c = app.test_client()
    tok = login(c)
    with app.app_context():
        users_before = User.query.count()
        nodes_before = Node.query.count()
    for method, path, meta in ROUTES:
        if method != 'GET' or '{' in path:
            continue
        for p in meta['params']:
            for payload in INJECTIONS:
                r = c.get(f'/api/v1{path}', query_string={p.name: payload}, headers=H(tok))
                assert r.status_code in (200, 400), (path, p.name, payload, r.status_code,
                                                     {k: v for k, v in r.headers.items() if 'Rate' in k})
                text = r.get_data(as_text=True)
                assert 'password_hash' not in text and '$2b$' not in text and 'Traceback' not in text
    with app.app_context():
        assert User.query.count() == users_before and Node.query.count() == nodes_before


def test_bodies_are_validated_strictly(app):
    c = app.test_client()
    tok = login(c)
    ok = {'name': 'new-node', 'ip_address': '10.1.1.1', 'username': 'pi', 'password': 'pw'}
    cases = [dict(ok, extra='x'), dict(ok, name="x'; DROP TABLE nodes;--"), dict(ok, name='<b>x</b>'),
             dict(ok, ssh_port=70000), dict(ok, ssh_port='22'), dict(ok, username='pi; rm -rf /'),
             dict(ok, ip_address='10.0.0.1; reboot'), {'name': 'x'}]
    for body in cases:
        r = c.post('/api/v1/nodes', headers=H(tok), json=body)
        assert r.status_code == 400 and r.get_json()['error'] == 'validation_error', body
    assert c.post('/api/v1/nodes', headers=H(tok), data='not json',
                  content_type='application/json').status_code == 400
    assert c.post('/api/v1/nodes', headers=H(tok), data='a=b').status_code == 415
    r = c.post('/api/v1/nodes', headers=H(tok), json=ok)
    assert r.status_code == 201 and 'password' not in r.get_json()


def test_unknown_query_parameters_and_oversize_bodies(app):
    c = app.test_client()
    tok = login(c)
    assert c.get('/api/v1/nodes?per_page=99999', headers=H(tok)).status_code == 400
    assert c.get('/api/v1/nodes?sort=name', headers=H(tok)).status_code == 400
    app.config['API_MAX_BODY_BYTES'] = 1000
    r = c.post('/api/v1/nodes', headers=H(tok), json={'description': 'x' * 2000})
    assert r.status_code == 413


def test_no_response_ever_contains_secrets(app):
    from app.routes.api import ROUTES
    c = app.test_client()
    tok = login(c)
    for method, path, meta in ROUTES:
        if method != 'GET' or '{' in path or path.startswith('/exports'):
            continue
        r = c.get(f'/api/v1{path}' + ('?node_id=1' if path == '/metrics/history' else ''), headers=H(tok))
        text = r.get_data(as_text=True)
        for secret in ('node-secret-pw', 'admin123', 'password_hash', 'secret_hash', '_password', 'totp_secret'):
            assert secret not in text, (path, secret)


def test_security_headers_and_no_cookie_for_tokens(app):
    c = app.test_client()
    tok = login(c)
    r = c.get('/api/v1/nodes', headers=H(tok))
    assert r.headers['Cache-Control'] == 'no-store' and r.headers['X-Content-Type-Options'] == 'nosniff'
    assert 'Set-Cookie' not in r.headers


# ------------------------------------------------------- data and changes

def test_data_points_summary_and_history(app):
    from app.models import MetricsHourly
    c = app.test_client()
    tok = login(c)
    with app.app_context():
        nid = Node.query.filter_by(name='node-0').first().id
        db.session.add(MetricsHourly(node_id=nid, hour=datetime.utcnow().replace(minute=0, second=0, microsecond=0)
                                     - timedelta(hours=2), samples=60, online=60, offline=0, auth_failed=0, cpu_avg=12,
                                     cpu_max=40))
        db.session.commit()
    pts = c.get('/api/v1/metrics/latest', headers=H(tok)).get_json()['points']
    assert {p['node_name'] for p in pts} == {'node-0', 'node-1', 'node-2'}
    s = c.get('/api/v1/fleet/summary', headers=H(tok)).get_json()
    assert s['fleet']['total'] == 3 and s['fleet']['online'] == 1
    assert {x['name'] for x in s['categories']} >= {'Pi Nodes', 'Edge Gateways'}
    h = c.get(f'/api/v1/metrics/history?node_id={nid}&interval=hour', headers=H(tok)).get_json()
    assert h['points'] and h['points'][0]['cpu_max'] == 40 and h['points'][0]['uptime_percent'] == 100
    assert c.get(f'/api/v1/metrics/history?node_id={nid}&from=2026-01-02T00:00:00Z&to=2026-01-01T00:00:00Z',
                 headers=H(tok)).status_code == 400
    f = c.get('/api/v1/nodes?status=offline', headers=H(tok)).get_json()
    assert [n['name'] for n in f['nodes']] == ['node-1'] and f['pagination']['total'] == 1
    f = c.get('/api/v1/nodes?category=Edge%20Gateways', headers=H(tok)).get_json()
    assert [n['name'] for n in f['nodes']] == ['node-1']


def test_event_feed_with_a_cursor(app):
    import app.services.collector as col
    c = app.test_client()
    tok = login(c)
    with app.app_context():
        n = Node.query.filter_by(name='node-0').first()
        coll = col.Collector()
        coll.run_cycle = coll.run_cycle                    # real method, fake SSH below
    orig = col.collect_one
    col.collect_one = lambda t: {'ok': False, 'kind': 'unreachable', 'message': 'No answer', 'seconds': 0.1}
    try:
        with app.app_context():
            coll.run_cycle(node_ids=[n.id], force=True)
    finally:
        col.collect_one = orig
    ev = c.get('/api/v1/events', headers=H(tok)).get_json()
    assert [(e['node_name'], e['from'], e['to']) for e in ev['events']] == [('node-0', 'online', 'offline')]
    again = c.get(f"/api/v1/events?since_id={ev['next_since_id']}", headers=H(tok)).get_json()
    assert again['events'] == [] and again['next_since_id'] == ev['next_since_id']


def test_write_endpoints_are_audited(app):
    from app.models import ActivityLog
    c = app.test_client()
    tok = login(c, 'op')
    r = c.post('/api/v1/nodes', headers=H(tok), json={'name': 'api-node', 'ip_address': '10.5.5.5', 'username': 'pi',
                                                     'password': 'pw'})
    nid = r.get_json()['id']
    c.patch(f'/api/v1/nodes/{nid}', headers=H(tok), json={'password': 'new-pw', 'location': 'roof'})
    c.delete(f'/api/v1/nodes/{nid}', headers=H(tok))
    with app.app_context():
        logs = [a.action + ' ' + (a.response or '') for a in ActivityLog.query.all()]
        assert any(l.startswith('Add Node') and 'API session' in l for l in logs)
        assert any('Edit Node' in l and 'and password' in l and 'new-pw' not in l for l in logs)
        assert Node.query.get(nid).is_deleted


def test_commands_are_off_unless_enabled_and_respect_the_blocklist(app, monkeypatch):
    import app.services.collector as col
    c = app.test_client()
    tok = login(c, 'op')
    with app.app_context():
        nid = Node.query.filter_by(name='node-0').first().id
    r = c.post('/api/v1/commands', headers=H(tok), json={'node_ids': [nid], 'command': 'uptime'})
    assert r.status_code == 403 and r.get_json()['error'] == 'commands_disabled'
    app.config['API_ALLOW_COMMANDS'] = True
    assert c.post('/api/v1/commands', headers=H(tok), json={'node_ids': [nid], 'command': 'rm -rf /'}).status_code == 403
    monkeypatch.setattr(col, 'run_command', lambda t, cmd, **k: {'stdout': 'up 3 days', 'exit_code': 0, 'success': True,
                                                                  'duration': 0.1, 'error': None})
    r = c.post('/api/v1/commands', headers=H(tok), json={'node_ids': [nid], 'command': 'uptime'})
    body = r.get_json()
    assert r.status_code == 200 and body['results'][0]['output'] == 'up 3 days'
    mon = login(c, 'mon')
    assert c.post('/api/v1/commands', headers=H(mon), json={'node_ids': [nid], 'command': 'uptime'}).status_code == 403


# ------------------------------------------------------------ documentation

def test_openapi_documents_every_route(app):
    c = app.test_client()
    spec = c.get('/api/v1/openapi.json').get_json()
    assert spec['openapi'].startswith('3.') and spec['components']['securitySchemes']['bearer']
    documented = {(m.upper(), p) for p, ops in spec['paths'].items() for m in ops}
    import re
    actual = set()
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith('/api/v1/') and rule.rule != '/api/v1/openapi.json':
            path = re.sub(r'<(?:int:)?(\w+)>', r'{\1}', rule.rule[len('/api/v1'):])
            for m in rule.methods - {'HEAD', 'OPTIONS'}:
                actual.add((m, path))
    assert actual == documented, f'undocumented: {actual - documented}, stale: {documented - actual}'
    for p, ops in spec['paths'].items():
        for m, op in ops.items():
            assert op.get('summary') and op.get('responses'), (m, p)
