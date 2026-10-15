"""Administrators control the API: a policy (on/off, which parts, service
keys), per-user API access, every credential with revoke, and a usage log of
every request (who, which credential, which endpoint, result). App deletes
leave the data lake alone; without a lake they delete as before."""
import pytest

from app import create_app, db, limiter
from app.models.api import ApiRequest, ApiToken
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
        for name, role in (('op', 'operator'), ('mon', 'monitor')):
            u = User(username=name, email=f'{name}@fleet.test', role=role, is_active=True, must_change_password=False)
            u.set_password('Operator-pass-1')
            db.session.add(u)
        n = Node(name='node-1', ip_address='10.0.0.1', username='pi', status='online')
        n.password = 'node-secret-pw'
        db.session.add(n)
        db.session.commit()
    yield app


def api_login(c, user='op', pw='Operator-pass-1'):
    r = c.post('/api/v1/auth/login', json={'username': user, 'password': pw})
    return r.get_json().get('token'), r


def H(t):
    return {'Authorization': f'Bearer {t}'}


def admin(app):
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    return c


def make_key(c, tok, **kw):
    body = dict({'name': 'erp', 'purpose': 'hourly sync into the ERP', 'scopes': ['read'],
                 'password': 'Operator-pass-1', 'code': '000000'}, **kw)
    return c.post('/api/v1/keys', headers=H(tok), json=body)


def test_only_administrators_open_the_api_access_page(app):
    op = app.test_client()
    op.post('/login', data={'username': 'op', 'password': 'Operator-pass-1'})
    assert op.get('/users/api').status_code in (302, 403)
    adm = admin(app)
    c = app.test_client()
    tok, _ = api_login(c)
    make_key(c, tok)
    c.get('/api/v1/nodes', headers=H(tok))
    page = adm.get('/users/api').get_data(as_text=True)
    assert 'hourly sync into the ERP' in page and 'erp' in page          # who made which key, for what
    assert 'GET /api/v1/nodes' in page and 'op' in page                   # the usage log
    assert 'node-secret-pw' not in page and 'secret_hash' not in page


def test_keys_need_a_purpose(app):
    c = app.test_client()
    tok, _ = api_login(c)
    r = make_key(c, tok, purpose='')
    assert r.status_code == 400
    assert make_key(c, tok).status_code == 201


def test_the_policy_can_turn_the_api_or_parts_of_it_off(app):
    adm = admin(app)
    c = app.test_client()
    tok, _ = api_login(c)
    with app.app_context():
        nid = Node.query.first().id
    adm.post('/users/api/policy', data={'enabled': 'on', 'scopes': ['read'], 'keys': 'on'})   # write off
    assert c.get('/api/v1/nodes', headers=H(tok)).status_code == 200
    r = c.patch(f'/api/v1/nodes/{nid}', headers=H(tok), json={'location': 'x'})
    assert r.status_code == 403 and r.get_json()['error'] == 'scope_disabled'
    adm.post('/users/api/policy', data={'enabled': 'on', 'scopes': ['read', 'write'], 'keys': ''})  # keys off
    r = make_key(c, tok)
    assert r.status_code == 403 and r.get_json()['error'] == 'keys_disabled'
    adm.post('/users/api/policy', data={'scopes': ['read']})                                      # API off
    r = c.get('/api/v1/nodes', headers=H(tok))
    assert r.status_code == 403 and r.get_json()['error'] == 'api_disabled'
    assert api_login(app.test_client())[1].status_code == 403                                     # no sign-in
    assert app.test_client().get('/api/v1/health').status_code == 200                             # health stays
    from app.models import ActivityLog
    with app.app_context():
        assert ActivityLog.query.filter_by(action='API policy changed').count() == 3


def test_blocking_a_user_ends_every_credential(app):
    adm = admin(app)
    c = app.test_client()
    tok, _ = api_login(c)
    key = make_key(c, tok).get_json()['key']
    with app.app_context():
        uid = User.query.filter_by(username='op').first().id
    adm.post(f'/users/api/user/{uid}/access', data={'allow': '0'})
    assert c.get('/api/v1/nodes', headers=H(tok)).status_code == 401
    assert c.get('/api/v1/nodes', headers=H(key)).status_code == 401
    tok2, r = api_login(c)
    assert r.status_code == 403 and r.get_json()['error'] == 'api_blocked'
    web = app.test_client()                                     # the web app is not affected
    web.post('/login', data={'username': 'op', 'password': 'Operator-pass-1'})
    assert web.get('/nodes/').status_code == 200
    adm.post(f'/users/api/user/{uid}/access', data={'allow': '1'})
    assert api_login(app.test_client())[1].status_code == 200


def test_administrators_revoke_any_credential(app):
    adm = admin(app)
    c = app.test_client()
    tok, _ = api_login(c)
    key = make_key(c, tok).get_json()['key']
    with app.app_context():
        kid = ApiToken.query.filter_by(kind='key').first().id
    adm.post(f'/users/api/credential/{kid}/revoke')
    assert c.get('/api/v1/nodes', headers=H(key)).status_code == 401
    with app.app_context():
        assert ApiToken.query.get(kid).revoked_reason == 'revoked_by_admin'


def test_every_request_is_logged_without_values(app):
    c = app.test_client()
    c.post('/api/v1/auth/login', json={'username': 'op', 'password': 'wrong-password'})
    tok, _ = api_login(c)
    c.get('/api/v1/nodes?search=topsecretvalue', headers=H(tok))
    c.get('/api/v1/nodes/999', headers=H(tok))
    with app.app_context():
        rows = ApiRequest.query.order_by(ApiRequest.id).all()
        text = ' '.join(f'{r.username} {r.credential} {r.endpoint} {r.status}' for r in rows)
        assert any(r.endpoint == '/api/v1/auth/login' and r.status == 401 and r.username == '(sign-in as op)'
                   for r in rows)
        assert any(r.endpoint == '/api/v1/nodes' and r.username == 'op' and (r.credential or '').startswith('session')
                   for r in rows)
        assert any(r.endpoint == '/api/v1/nodes/{node_id}' and r.status == 404 for r in rows)
        assert 'wrong-password' not in text and 'topsecretvalue' not in text and 'Operator-pass-1' not in text


def test_deletes_rename_first_so_names_can_be_reused(app):
    adm = admin(app)
    with app.app_context():
        nid = Node.query.first().id
        uid = User.query.filter_by(username='mon').first().id
    adm.post(f'/nodes/{nid}/delete')
    adm.post(f'/nodes/{nid}/purge')
    adm.post(f'/users/{uid}/delete')
    with app.app_context():
        assert Node.query.get(nid) is None and User.query.get(uid) is None
    c = app.test_client()
    tok, _ = api_login(c)
    r = c.post('/api/v1/nodes', headers=H(tok), json={'name': 'node-1', 'ip_address': '10.0.0.5',
                                                     'username': 'pi', 'password': 'x'})
    assert r.status_code == 201, r.get_json()                                    # the name is free again


def test_a_user_with_support_tickets_is_not_deleted(app):
    from app.models.issue import Issue
    adm = admin(app)
    with app.app_context():
        u = User.query.filter_by(username='mon').first()
        db.session.add(Issue(subject='help', reporter_id=u.id))
        db.session.commit()
        uid = u.id
    r = adm.post(f'/users/{uid}/delete', follow_redirects=True)
    assert 'Deactivate it instead' in r.get_data(as_text=True)
    with app.app_context():
        assert User.query.get(uid) is not None


def _second_admin(app):
    with app.app_context():
        u = User(username='adm2', email='adm2@fleet.test', role='administrator', is_active=True, must_change_password=False)
        u.set_password('Operator-pass-1')
        db.session.add(u)
        db.session.commit()
        return u.id


def test_only_the_owner_changes_the_api_policy(app):
    _second_admin(app)
    c = app.test_client()
    c.post('/login', data={'username': 'adm2', 'password': 'Operator-pass-1'})
    c.post('/users/api/policy', data={'scopes': ['read']})                 # would turn the API off
    from app.services import api_auth
    with app.app_context():
        assert api_auth.policy()['enabled'] is True
    assert 'Only the owner can change the policy' in c.get('/users/api').get_data(as_text=True)


def test_an_administrator_cannot_lift_a_block_on_themselves(app):
    uid = _second_admin(app)
    admin(app).post(f'/users/api/user/{uid}/access', data={'allow': '0'})       # the owner blocks adm2
    c = app.test_client()
    c.post('/login', data={'username': 'adm2', 'password': 'Operator-pass-1'})
    c.post(f'/users/api/user/{uid}/access', data={'allow': '1'})
    with app.app_context():
        assert db.session.get(User, uid).api_access is False
    c.post(f'/users/api/user/{uid}/access', data={'allow': '0'})               # blocking yourself still works


def test_unknown_paths_and_typed_passwords_never_reach_the_log(app):
    c = app.test_client()
    for i in range(5):
        c.get(f'/api/v1/no-such-thing-{i}')
    c.post('/api/v1/nodes/1/nothing')
    c.post('/api/v1/auth/login', json={'username': 'MyS3cretPassw0rd', 'password': 'x'})
    with app.app_context():
        rows = ApiRequest.query.all()
        assert not any('no-such-thing' in (r.endpoint or '') for r in rows)
        assert 'MyS3cretPassw0rd' not in ' '.join(r.username or '' for r in rows)
        assert any(r.username == '(unknown user)' for r in rows)


def test_a_failed_request_is_logged_without_committing_its_changes(app):
    from app.models.api import ApiRequest as R
    c = app.test_client()
    tok, _ = api_login(c)
    with app.app_context():
        before = R.query.count()
    c.get('/api/v1/nodes', headers=H(tok))
    with app.app_context():
        assert R.query.count() == before + 1


def test_api_admins_cannot_revoke_the_owners_keys(app):
    _second_admin(app)
    with app.app_context():
        from app.services import api_auth
        api_auth.save_policy(True, ['read', 'write', 'control', 'admin'], True)
    owner = app.test_client()
    otok, _ = api_login(owner, 'admin', 'admin123')
    okey = make_key(owner, otok, password='admin123').get_json()
    c = app.test_client()
    tok, _ = api_login(c, 'adm2')
    r = c.delete(f"/api/v1/keys/{okey['details']['id']}", headers=H(tok))
    assert r.status_code == 403
    assert c.get('/api/v1/nodes', headers=H(okey['key'])).status_code == 200


@pytest.mark.parametrize('name', ['op', 'mon'])
def test_the_activity_log_is_for_administrators_only(app, name):
    from app.models import ActivityLog
    with app.app_context():
        db.session.add(ActivityLog(action='secret audit entry', status='failure'))
        db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': name, 'password': 'Operator-pass-1'})
    r = c.get('/logs/activity')
    assert r.status_code == 302 and 'secret audit entry' not in r.get_data(as_text=True)
    dash = c.get('/', follow_redirects=True).get_data(as_text=True)
    assert 'Activity Log' not in dash and 'secret audit entry' not in dash and 'Recent Activity' not in dash
    tok, _ = api_login(app.test_client(), name)
    r = app.test_client().get('/api/v1/activity', headers=H(tok))
    assert r.status_code == 403
    adm = admin(app)
    assert 'secret audit entry' in adm.get('/logs/activity').get_data(as_text=True)
    page = adm.get('/', follow_redirects=True).get_data(as_text=True)
    assert 'Activity Log' in page and 'Recent Activity' in page


@pytest.mark.parametrize('name', ['op', 'mon', 'admin'])
def test_every_role_reaches_the_api_page_from_the_sidebar(app, name):
    c = app.test_client()
    c.post('/login', data={'username': name, 'password': 'admin123' if name == 'admin' else 'Operator-pass-1'})
    home = c.get('/', follow_redirects=True).get_data(as_text=True)
    assert 'href="/account/api"' in home
    page = c.get('/account/api').get_data(as_text=True)
    assert 'Your access' in page and 'Create a service key' in page and '/api/v1' in page
    assert ('href="/users/api"' in page) == (name == 'admin')          # administration link for admins only
    assert 'Create a service key' not in c.get('/account/').get_data(as_text=True)


def test_the_api_guide_is_a_readable_page_for_every_signed_in_user(app):
    assert app.test_client().get('/account/api/guide').status_code == 302          # sign-in first
    c = app.test_client()
    c.post('/login', data={'username': 'mon', 'password': 'Operator-pass-1'})
    page = c.get('/account/api/guide').get_data(as_text=True)
    assert '<title>EdgeShell API guide</title>' in page and 'class="guide-toc"' in page
    assert 'id="signing-in"' in page and '<table' in page                          # docs/API.md, rendered
    assert 'href="/account/api/guide"' in c.get('/account/api').get_data(as_text=True)
