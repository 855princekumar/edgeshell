"""The SenseHive gate: only administrators, only with a pass from the admin
panel (a POST with the CSRF token) or a fresh second factor; the pass is tied
to the EdgeShell session that got it and dies with it."""
import pytest

from app import create_app, db, limiter
from app.models.user import User


@pytest.fixture
def app():
    app = create_app('testing')
    app.config.update(SENSEHIVE_PUBLIC_PATH='/sensehive/')
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
        db.session.commit()
    yield app


def login(app, user='admin', pw='admin123'):
    c = app.test_client()
    c.post('/login', data={'username': user, 'password': pw})
    return c


def has_pass(c):
    return c.get_cookie('es_sensehive', path='/sensehive/') is not None


def check(c):
    """nginx forwards the /sensehive/ request's cookies to the admin check; the
    test client sends a path-scoped cookie only to its path, so copy it."""
    p = c.get_cookie('es_sensehive', path='/sensehive/')
    if p is not None:
        c.set_cookie('es_sensehive', p.value, path='/internal/')
    else:
        c.delete_cookie('es_sensehive', path='/internal/')
    return c.get('/internal/admin-check').status_code


def test_without_a_pass_even_an_admin_is_sent_to_the_unlock_step(app):
    assert check(app.test_client()) == 401                 # not signed in
    assert check(login(app, 'op', 'Operator-pass-1')) == 403
    assert check(login(app)) == 401                        # admin, but no pass yet


def test_the_admin_panel_button_gives_a_pass(app):
    c = login(app)
    r = c.post('/tools/sensehive/launch')
    assert r.status_code == 302 and r.headers['Location'].endswith('/sensehive/')
    cookie = r.headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Path=/sensehive/' in cookie
    assert has_pass(c) and check(c) == 204


def test_the_button_needs_the_pages_csrf_token(app):
    app.config['WTF_CSRF_ENABLED'] = True
    c = login(app)
    assert c.post('/tools/sensehive/launch').status_code == 400       # a link or another site cannot do it
    assert not has_pass(c)
    assert c.get('/tools/sensehive/launch').status_code == 405


def test_operators_get_no_pass(app):
    c = login(app, 'op', 'Operator-pass-1')
    c.post('/tools/sensehive/launch')
    assert not has_pass(c) and check(c) == 403
    assert c.get('/tools/sensehive/unlock').status_code == 302


def test_a_direct_visit_needs_a_fresh_confirmation(app):
    c = login(app)
    page = c.get('/tools/sensehive/unlock').get_data(as_text=True)
    assert 'Open SenseHive' in page and 'name="password"' in page   # 2FA off in tests: the password instead
    r = c.post('/tools/sensehive/unlock', data={'password': 'wrong'})
    assert not has_pass(c) and 'not correct' in r.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(username='admin').first().failed_logins == 1   # counts toward the lockout
    r = c.post('/tools/sensehive/unlock', data={'password': 'admin123'})
    assert r.status_code == 302 and has_pass(c) and check(c) == 204


def test_with_2fa_on_the_unlock_step_asks_for_a_code(app, monkeypatch):
    from app.services import mfa_service as mfa
    monkeypatch.setattr(mfa, 'is_enrolled', lambda u: True)
    monkeypatch.setattr(mfa, 'verify', lambda u, code: 'totp' if code == '123456' else None)
    c = login(app)                                   # signed in (2FA off for the sign-in in tests) ...
    monkeypatch.setattr(mfa, 'mfa_required', lambda: True)   # ... then the unlock step sees 2FA on
    assert 'Two-factor code' in c.get('/tools/sensehive/unlock').get_data(as_text=True)
    c.post('/tools/sensehive/unlock', data={'code': '000000'})
    assert not has_pass(c)
    c.post('/tools/sensehive/unlock', data={'code': '123456'})
    assert has_pass(c) and check(c) == 204


def test_a_pass_is_useless_in_another_session_and_dies_at_sign_out(app):
    a = login(app)
    a.post('/tools/sensehive/launch')
    stolen = a.get_cookie('es_sensehive', path='/sensehive/').value
    a.get('/logout')
    assert check(a) == 401                                  # signed out: the pass is dead
    b = login(app)                                          # a new session for the same admin
    b.set_cookie('es_sensehive', stolen, path='/sensehive/')
    assert check(b) == 401                                  # not this session's pass
    b.set_cookie('es_sensehive', 'forged.' + stolen, path='/sensehive/')
    assert check(b) == 401
    b.post('/tools/sensehive/launch')                       # this session's own pass works
    assert check(b) == 204


def test_the_sidebar_button_is_a_form_for_administrators_only(app):
    page = login(app).get('/nodes/').get_data(as_text=True)
    assert 'action="/tools/sensehive/launch"' in page and 'target="_blank"' in page
    assert '/tools/sensehive/launch' not in login(app, 'op', 'Operator-pass-1').get('/nodes/').get_data(as_text=True)
