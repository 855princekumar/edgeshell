"""Mandatory 2FA, account recovery, admin governance, sign-up approval and
device sessions. Uses the built-in verifier; the privacyIDEA client is tested
against a fake server (and for real in the sandbox E2E)."""
import re
from datetime import datetime, timedelta

import pytest

from app import create_app, db
from app.models.user import PasswordResetRequest, User, UserMFA, UserSession
from app.services import mfa_service as mfa


@pytest.fixture
def app():
    app = create_app('testing')
    app.config['MFA_REQUIRED'] = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        from app import _seed_defaults
        from app.services.account_service import ensure_owner
        _seed_defaults()
        ensure_owner()
        u = User.query.filter_by(username='admin').first()
        u.must_change_password = False
        u.email = 'owner@fleet.test'
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()


def _mk(app, name, role='operator', **kw):
    with app.app_context():
        from app.services.account_service import apply_role
        u = User(username=name, email=f'{name}@fleet.test', role='monitor', is_active=True,
                 must_change_password=False, max_sessions=kw.pop('max_sessions', 1), **kw)
        apply_role(u, role)
        u.set_password('pw-' + name + '-123')
        db.session.add(u)
        db.session.commit()
        return u.id


def _secret_from(html):
    m = re.search(r'id="secret">([A-Z2-7 ]+)<', html)
    return m.group(1).replace(' ', '')


def _enroll(client, username, password):
    """Password, then 2FA setup. Returns (secret, backup codes)."""
    r = client.post('/login', data={'username': username, 'password': password})
    assert r.status_code == 302 and '/login/setup' in r.headers['Location']
    secret = _secret_from(client.get('/login/setup').get_data(as_text=True))
    r = client.post('/login/setup', data={'code': mfa.totp_now(secret)})
    body = r.get_data(as_text=True)
    assert 'Your backup codes' in body
    codes = re.findall(r'<span>([a-z2-9]{5}-[a-z2-9]{5})</span>', body)
    assert len(codes) == mfa.BACKUP_CODES
    r = client.post('/login/setup/done', data={'saved': '1'})
    assert r.status_code == 302
    return secret, codes


def _login_2fa(client, username, password, code):
    r = client.post('/login', data={'username': username, 'password': password})
    assert r.status_code == 302 and '/login/verify' in r.headers['Location'], r.headers
    return client.post('/login/verify', data={'code': code})


# ---------------------------------------------------------------- 2FA

def test_first_login_requires_2fa_setup_then_code(app):
    c = app.test_client()
    secret, codes = _enroll(c, 'admin', 'admin123')
    assert c.get('/').status_code == 200
    c.get('/logout')

    # Next sign-in asks for a code; nothing is signed in before it.
    r = c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    assert '/login/verify' in r.headers['Location']
    assert c.get('/').status_code == 302
    r = c.post('/login/verify', data={'code': mfa.totp_now(secret, step_offset=1)})
    assert r.status_code == 302 and c.get('/').status_code == 200


def test_enrollment_qr_scales_instead_of_cropping():
    """The page sets the QR's size with CSS. Without a viewBox the browser
    crops the code (losing its corner markers) instead of scaling it, and no
    authenticator app can read it."""
    svg = mfa.qr_svg('otpauth://totp/EdgeShell:admin?secret=JBSWY3DPEHPK3PXP')
    head = svg[:svg.index('>')]
    assert 'viewBox' in head and 'width=' not in head and 'height=' not in head


def test_totp_replay_and_wrong_code_rejected(app):
    c = app.test_client()
    secret, _ = _enroll(c, 'admin', 'admin123')
    c.get('/logout')
    with app.app_context():                # the exact step the enrollment consumed
        step = db.session.get(UserMFA, 1).last_step
    used = mfa.totp_now(secret, at=step * mfa.TOTP_STEP)
    r = _login_2fa(c, 'admin', 'admin123', used)
    assert b'not valid' in r.data
    r = c.post('/login/verify', data={'code': '000000' if used != '000000' else '111111'})
    assert b'not valid' in r.data and c.get('/').status_code == 302


def test_backup_code_works_once(app):
    c = app.test_client()
    _, codes = _enroll(c, 'admin', 'admin123')
    c.get('/logout')
    r = _login_2fa(c, 'admin', 'admin123', codes[0].upper())   # case and dash tolerant
    assert r.status_code == 302 and c.get('/').status_code == 200
    with app.app_context():
        assert mfa.backup_codes_left(db.session.get(User, 1)) == mfa.BACKUP_CODES - 1
    c.get('/logout')
    r = _login_2fa(c, 'admin', 'admin123', codes[0])
    assert b'not valid' in r.data


def test_2fa_failures_lock_the_account(app):
    c = app.test_client()
    _enroll(c, 'admin', 'admin123')
    c.get('/logout')
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    for _ in range(5):
        c.post('/login/verify', data={'code': '999999'})
    with app.app_context():
        assert db.session.get(User, 1).locked_until > datetime.utcnow()
    r = c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    assert b'temporarily locked' in r.data


def test_secrets_stay_encrypted_and_hashed(app):
    c = app.test_client()
    secret, codes = _enroll(c, 'admin', 'admin123')
    with app.app_context():
        row = db.session.get(UserMFA, 1)
        assert secret not in row.totp_ref and row.provider == 'local'
        assert codes[0] not in row.backup_ref and codes[0].replace('-', '') not in row.backup_ref


def test_privacyidea_client_flow(app, monkeypatch):
    """EdgeShell talks to privacyIDEA: service JWT, token init, validate by serial."""
    app.config.update(MFA_PROVIDER='privacyidea', PRIVACYIDEA_URL='http://pi:8080',
                      PRIVACYIDEA_PASSWORD='svc')
    mfa.PrivacyIDEA._token = None
    tokens, calls = {}, []

    class R:
        def __init__(self, status, body):
            self.status_code, self._b = status, body

        def json(self):
            return self._b

    def fake(method, url, timeout=None, verify=None, headers=None, data=None, params=None):
        path = url.replace('http://pi:8080', '')
        calls.append((method, path, (headers or {}).get('Authorization')))
        ok = {'result': {'status': True, 'value': True}}
        if path == '/auth':
            return R(200, {'result': {'status': True, 'value': {'token': 'jwt-1'}}})
        if path == '/token/init':
            tokens[data['serial']] = data
            if data['type'] == 'totp':
                return R(200, dict(ok, detail={'googleurl': {
                    'value': 'otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&counter=0'}}))
            return R(200, ok)
        if path == '/validate/check':
            t = tokens.get(data['serial'])
            good = bool(t) and (data['pass'] == mfa.totp_now('JBSWY3DPEHPK3PXP')
                                if t['type'] == 'totp' else data['pass'] in t['tans'].split())
            return R(200, {'result': {'status': True, 'value': good}, 'detail': {}})
        if method == 'DELETE':
            tokens.pop(path.rsplit('/', 1)[1], None)
            return R(200, ok)
        if path == '/token/':
            return R(200, {'result': {'status': True, 'value': {'tokens': [
                {'info': {'tan.count': '9'}}]}}})
        return R(404, {'result': {'status': False}})

    monkeypatch.setattr(mfa.requests, 'request', fake)
    c = app.test_client()
    secret, codes = _enroll(c, 'admin', 'admin123')
    assert secret == 'JBSWY3DPEHPK3PXP'
    with app.app_context():
        row = db.session.get(UserMFA, 1)
        assert row.provider == 'privacyidea' and row.totp_ref.startswith('ES1T')
        assert 'JBSWY' not in row.totp_ref                 # only the serial is stored
        assert mfa.backup_codes_left(db.session.get(User, 1)) == 9
    assert all(a == 'jwt-1' for m, p, a in calls if p.startswith('/token'))
    assert all(a is None for m, p, a in calls if p == '/validate/check')

    c.get('/logout')
    r = _login_2fa(c, 'admin', 'admin123', codes[2])
    assert r.status_code == 302 and c.get('/').status_code == 200


def test_privacyidea_outage_fails_closed(app, monkeypatch):
    app.config.update(MFA_PROVIDER='privacyidea', PRIVACYIDEA_URL='http://pi:8080')
    mfa.PrivacyIDEA._token = None

    def down(*a, **k):
        raise mfa.requests.ConnectionError('down')
    monkeypatch.setattr(mfa.requests, 'request', down)
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    r = c.get('/login/setup', follow_redirects=True)
    assert b'not responding' in r.data and c.get('/').status_code == 302


# ---------------------------------------------------------------- resets

def test_admin_reset_removes_2fa_and_ends_sessions(app):
    uid = _mk(app, 'olly')
    u = app.test_client()
    _enroll(u, 'olly', 'pw-olly-123')
    with app.app_context():
        db.session.add(PasswordResetRequest(user_id=uid))
        db.session.commit()
    a = app.test_client()
    _enroll(a, 'admin', 'admin123')
    a.post(f'/users/{uid}/reset-password', data={'new_password': 'Temp-pass-9x'})
    assert u.get('/', headers={'Accept': 'application/json'}).status_code == 401
    with app.app_context():
        assert db.session.get(UserMFA, uid) is None
        assert PasswordResetRequest.query.filter_by(user_id=uid).first().status == 'done'
    # Next sign-in: set up 2FA again, then forced password change.
    _enroll(u, 'olly', 'Temp-pass-9x')
    r = u.get('/')
    assert r.status_code == 302 and '/change-password' in r.headers['Location']


def test_first_admin_order_is_2fa_then_email_then_password(app):
    with app.app_context():
        u = db.session.get(User, 1)
        u.email, u.must_change_password = 'admin@edgeshell.local', True
        db.session.commit()
    c = app.test_client()
    r = c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    assert '/login/setup' in r.headers['Location']              # 1. 2FA
    secret = _secret_from(c.get('/login/setup').get_data(as_text=True))
    c.post('/login/setup', data={'code': mfa.totp_now(secret)})
    r = c.post('/login/setup/done', data={'saved': '1'})
    assert '/account/email' in r.headers['Location']            # 2. personal email
    assert '/account/email' in c.get('/nodes/').headers['Location']
    r = c.post('/account/email', data={'email': 'me@fleet.test', 'password': 'admin123'})
    assert '/change-password' in r.headers['Location']          # 3. new password
    c.post('/change-password', data={'current_password': 'admin123', 'new_password': 'My-own-pass-1',
                                     'confirm_password': 'My-own-pass-1'})
    assert c.get('/nodes/').status_code == 200


def test_forgot_password_user_creates_one_request(app):
    _mk(app, 'olly')
    c = app.test_client()
    for _ in range(2):
        r = c.post('/forgot', data={'username': 'olly'}, follow_redirects=True)
        assert b'If that account exists' in r.data
    r = c.post('/forgot', data={'username': 'nobody'}, follow_redirects=True)
    assert b'If that account exists' in r.data              # same answer
    with app.app_context():
        assert PasswordResetRequest.query.count() == 1


def test_admin_email_reset_link_single_use(app, monkeypatch):
    app.config.update(SMTP_HOST='smtp.test', SMTP_FROM='es@fleet.test',
                      PUBLIC_URL='https://fleet.test')
    sent = []
    import app.services.email_service as es
    monkeypatch.setattr(es, '_send_smtp', lambda to, subject, text: sent.append((to, text)) or (True, 'sent'))
    from datetime import datetime
    with app.app_context():
        db.session.get(User, 1).email_verified_at = datetime.utcnow()   # links go to verified addresses only
        db.session.commit()
    c = app.test_client()
    c.post('/forgot', data={'username': 'admin'})
    assert sent and sent[0][0] == 'owner@fleet.test'
    link = re.search(r'https://fleet\.test(/reset/\S+)', sent[0][1]).group(1)
    r = c.post(link, data={'new_password': 'Brand-new-pass', 'confirm_password': 'Brand-new-pass'})
    assert r.status_code == 302 and '/login' in r.headers['Location']
    with app.app_context():
        assert db.session.get(User, 1).check_password('Brand-new-pass')
    r = c.post(link, data={'new_password': 'second-reset-attempt', 'confirm_password': 'second-reset-attempt'})
    assert '/forgot' in r.headers['Location']                 # link already used


def test_the_reset_page_asks_for_the_new_password_and_a_2fa_code(app, monkeypatch):
    """One page: the link proves the mailbox, the code proves the device."""
    app.config.update(SMTP_HOST='smtp.test', SMTP_FROM='es@fleet.test', PUBLIC_URL='https://fleet.test')
    sent = []
    import app.services.email_service as es
    monkeypatch.setattr(es, '_send_smtp', lambda to, subject, text: sent.append((to, text)) or (True, 'sent'))
    c = app.test_client()
    secret, codes = _enroll(c, 'admin', 'admin123')
    c.get('/logout')
    from datetime import datetime
    with app.app_context():
        db.session.get(User, 1).email_verified_at = datetime.utcnow()
        db.session.commit()
    c = app.test_client()
    c.post('/forgot', data={'username': 'admin'})
    link = re.search(r'https://fleet\.test(/reset/\S+)', sent[-1][1]).group(1)
    page = c.get(link).get_data(as_text=True)
    assert 'name="new_password"' in page and 'name="code"' in page          # both on one page
    form = {'new_password': 'Brand-new-pass', 'confirm_password': 'Brand-new-pass'}
    r = c.post(link, data=dict(form, code='000000'))
    assert r.status_code == 200 and 'That code is not valid' in r.get_data(as_text=True)
    with app.app_context():
        u = db.session.get(User, 1)
        assert u.check_password('admin123') and u.failed_logins == 1      # unchanged, counted
    r = c.post(link, data={'new_password': 'short', 'confirm_password': 'short', 'code': codes[0]})
    with app.app_context():
        assert db.session.get(User, 1).check_password('admin123')          # a typo does not spend the code
    r = c.post(link, data=dict(form, code=codes[0]))
    assert r.status_code == 302 and '/login' in r.headers['Location']
    with app.app_context():
        assert db.session.get(User, 1).check_password('Brand-new-pass')
    r = c.post(link, data=dict(form, code=codes[1]))
    assert '/forgot' in r.headers['Location']                                # single use


def test_no_reset_mail_without_public_url(app, monkeypatch):
    app.config.update(SMTP_HOST='smtp.test', SMTP_FROM='es@fleet.test', PUBLIC_URL='')
    sent = []
    import app.services.email_service as es
    monkeypatch.setattr(es, '_send_smtp', lambda *a: sent.append(a) or (True, 'sent'))
    app.test_client().post('/forgot', data={'username': 'admin'})
    assert sent == []          # links are never built from the Host header


def test_admin_recovers_with_backup_code(app):
    c = app.test_client()
    secret, codes = _enroll(c, 'admin', 'admin123')
    c.get('/logout')
    form = {'username': 'admin', 'new_password': 'Recovered-pass', 'confirm_password': 'Recovered-pass'}
    r = c.post('/recover', data=dict(form, code=mfa.totp_now(secret)))
    assert b'backup code here' in r.data                      # app codes are not accepted
    r = c.post('/recover', data=dict(form, code=codes[4]))
    assert r.status_code == 302
    r = _login_2fa(c, 'admin', 'Recovered-pass', codes[5])
    assert c.get('/').status_code == 200


def test_backup_recovery_is_for_admins_only(app):
    _mk(app, 'olly')
    c = app.test_client()
    _, codes = _enroll(c, 'olly', 'pw-olly-123')
    c.get('/logout')
    r = c.post('/recover', data={'username': 'olly', 'code': codes[0], 'new_password': 'Nope-pass-123',
                                 'confirm_password': 'Nope-pass-123'})
    assert b'do not match' in r.data


# ---------------------------------------------------------------- governance

def _admin_client(app, name='admin', password='admin123'):
    c = app.test_client()
    _enroll(c, name, password)
    return c


def test_at_most_three_admins_and_only_owner_grants(app):
    a2 = _mk(app, 'adm2', role='administrator')
    owner = _admin_client(app)
    peer = _admin_client(app, 'adm2', 'pw-adm2-123')
    op = _mk(app, 'olly')
    r = peer.post(f'/users/{op}/role', data={'role': 'administrator'}, follow_redirects=True)
    assert b'Only the owner' in r.data
    owner.post(f'/users/{op}/role', data={'role': 'administrator'})
    with app.app_context():
        assert db.session.get(User, op).is_admin()
    four = _mk(app, 'fourth')
    r = owner.post(f'/users/{four}/role', data={'role': 'administrator'}, follow_redirects=True)
    assert b'at most 3' in r.data
    with app.app_context():
        assert not db.session.get(User, four).is_admin() and db.session.get(User, a2).is_admin()


def test_admins_cannot_touch_other_admins_or_the_owner(app):
    a3 = _mk(app, 'adm3', role='administrator')
    _mk(app, 'adm2', role='administrator')
    peer = _admin_client(app, 'adm2', 'pw-adm2-123')
    for target in (a3, 1):                                     # another admin, the owner
        for path, data in ((f'/users/{target}/role', {'role': 'monitor'}), (f'/users/{target}/toggle', {}),
                           (f'/users/{target}/reset-password', {'new_password': 'Takeover-123'}),
                           (f'/users/{target}/signout', {}), (f'/users/{target}/delete', {}),
                           (f'/users/{target}/sessions', {'max_sessions': '4'})):
            r = peer.post(path, data=data, follow_redirects=True)
            assert b'nobody can change the owner' in r.data, (target, path)
    with app.app_context():
        for uid, pw in ((a3, 'pw-adm3-123'), (1, 'admin123')):
            u = db.session.get(User, uid)
            assert u.is_admin() and u.is_active and u.check_password(pw) and u.max_sessions == 1


def test_owner_has_full_control_over_admins(app):
    a2 = _mk(app, 'adm2', role='administrator')
    owner = _admin_client(app)
    owner.post(f'/users/{a2}/sessions', data={'max_sessions': '3'})
    owner.post(f'/users/{a2}/reset-password', data={'new_password': 'Owner-reset-1'})
    with app.app_context():
        u = db.session.get(User, a2)
        assert u.max_sessions == 3 and u.check_password('Owner-reset-1') and u.must_change_password
    owner.post(f'/users/{a2}/role', data={'role': 'operator'})        # demote
    owner.post(f'/users/{a2}/toggle')                                 # disable
    with app.app_context():
        u = db.session.get(User, a2)
        assert u.role == 'operator' and not u.is_active and u.admin_since is None
    owner.post(f'/users/{a2}/delete')
    with app.app_context():
        assert db.session.get(User, a2) is None


def test_step_down_rules_and_owner_handover(app):
    c = app.test_client()
    secret, codes = _enroll(c, 'admin', 'admin123')
    form = {'new_role': 'operator', 'password': 'admin123', 'confirm': 'STEP DOWN'}
    r = c.post('/account/step-down', data=dict(form, code=codes[0]), follow_redirects=True)
    assert b'only administrator' in r.data                    # last admin stays
    _mk(app, 'later', role='administrator')
    with app.app_context():                                    # make 'early' the longest-serving
        early = db.session.get(User, _mk(app, 'early', role='administrator'))
        early.admin_since = datetime.utcnow() - timedelta(days=30)
        db.session.commit()
    r = c.post('/account/step-down', data=dict(form, code='123123'), follow_redirects=True)
    assert b'code was not valid' in r.data
    r = c.post('/account/step-down', data=dict(form, code=codes[1]), follow_redirects=True)
    assert b'early is the new owner' in r.data
    with app.app_context():
        assert db.session.get(User, 1).role == 'operator' and not db.session.get(User, 1).is_owner
        assert User.query.filter_by(username='early').first().is_owner


def test_signup_waits_for_approval(app):
    c = app.test_client()
    r = c.post('/signup', data={'username': 'newbie', 'email': 'n@fleet.test',
                                'password': 'Newbie-pass-1', 'confirm_password': 'Newbie-pass-1'})
    assert r.status_code == 302
    r = c.post('/login', data={'username': 'newbie', 'password': 'Newbie-pass-1'})
    assert b'waiting for an administrator' in r.data
    with app.app_context():
        nid = User.query.filter_by(username='newbie').first().id
    _mk(app, 'adm2', role='administrator')
    peer = _admin_client(app, 'adm2', 'pw-adm2-123')
    r = peer.post(f'/users/{nid}/approve', data={'role': 'administrator'}, follow_redirects=True)
    assert b'Only the owner' in r.data
    peer.post(f'/users/{nid}/approve', data={'role': 'operator', 'max_sessions': '2'})
    with app.app_context():
        u = db.session.get(User, nid)
        assert u.is_active and u.role == 'operator' and u.max_sessions == 2
    _enroll(c, 'newbie', 'Newbie-pass-1')
    assert c.get('/terminal/').status_code == 200


def test_signup_can_be_rejected(app):
    c = app.test_client()
    c.post('/signup', data={'username': 'spam', 'email': 's@fleet.test',
                            'password': 'Spammy-pass-1', 'confirm_password': 'Spammy-pass-1'})
    with app.app_context():
        sid = User.query.filter_by(username='spam').first().id
    owner = _admin_client(app)
    owner.post(f'/users/{sid}/reject')
    with app.app_context():
        assert db.session.get(User, sid) is None


def test_default_admin_is_not_recreated(app):
    with app.app_context():
        u = db.session.get(User, 1)
        u.username = 'renamed'
        db.session.commit()
        from app import _seed_defaults
        _seed_defaults()
        assert User.query.filter_by(username='admin').first() is None


def test_admin_must_add_personal_email(app):
    with app.app_context():
        db.session.get(User, 1).email = 'admin@edgeshell.local'
        db.session.commit()
    c = _admin_client(app)
    r = c.get('/nodes/')
    assert r.status_code == 302 and '/account/email' in r.headers['Location']
    c.post('/account/email', data={'email': 'me@fleet.test', 'password': 'admin123'})
    assert c.get('/nodes/').status_code == 200


# ---------------------------------------------------------------- sessions

PHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148 Safari/604.1'


def test_devices_are_typed_and_closable(app):
    uid = _mk(app, 'olly', max_sessions=2)
    phone = app.test_client()
    phone.environ_base['HTTP_USER_AGENT'] = PHONE
    desk = app.test_client()
    desk.environ_base['HTTP_USER_AGENT'] = 'Mozilla/5.0 (Windows NT 10.0) Chrome/128.0'
    _enroll(phone, 'olly', 'pw-olly-123')
    with app.app_context():
        from app.services.crypto import decrypt_value
        secret = decrypt_value(db.session.get(UserMFA, uid).totp_ref)
    _login_2fa(desk, 'olly', 'pw-olly-123', mfa.totp_now(secret, step_offset=1))
    with app.app_context():
        rows = {s.device_type: s for s in UserSession.query.filter_by(user_id=uid)}
        assert set(rows) == {'Mobile', 'Desktop'}
        phone_id = rows['Mobile'].id
        from app.models import ActivityLog
        assert ActivityLog.query.filter(ActivityLog.device.like('Mobile #%')).count() >= 1
    page = desk.get('/account/').get_data(as_text=True)
    assert 'Mobile #' in page and 'this device' in page
    desk.post(f'/account/sessions/{phone_id}/close')
    assert phone.get('/', headers={'Accept': 'application/json'}).status_code == 401
    assert desk.get('/').status_code == 200


def test_admin_closes_one_device_of_a_user(app):
    uid = _mk(app, 'olly', max_sessions=2)
    u = app.test_client()
    _enroll(u, 'olly', 'pw-olly-123')
    with app.app_context():
        sid = UserSession.query.filter_by(user_id=uid).first().id
    owner = _admin_client(app)
    assert b'#' in owner.get(f'/users/{uid}/devices').data
    owner.post(f'/users/{uid}/devices/{sid}/close')
    assert u.get('/', headers={'Accept': 'application/json'}).status_code == 401


def test_idle_session_is_signed_out(app):
    c = _admin_client(app)
    with app.app_context():
        s = UserSession.query.filter_by(user_id=1).first()
        s.active_at = datetime.utcnow() - timedelta(minutes=9)
        db.session.commit()
    # Polls do not count as activity.
    c.get('/session/status', headers={'Accept': 'application/json'})
    with app.app_context():
        assert UserSession.query.filter_by(user_id=1).first().active_at < datetime.utcnow() - timedelta(minutes=8)
    j = c.get('/session/status').get_json()
    assert j['idle_s'] < j['idle_after_s']
    with app.app_context():
        s = UserSession.query.filter_by(user_id=1).first()
        s.active_at = datetime.utcnow() - timedelta(minutes=11)
        db.session.commit()
    j = c.get('/session/status').get_json()
    assert j['idle_s'] >= j['idle_after_s']                   # the page shows the prompt
    c.post('/session/activity')                                # "I'm still here"
    assert c.get('/session/status').get_json()['idle_s'] < 5
    with app.app_context():
        s = UserSession.query.filter_by(user_id=1).first()
        s.active_at = datetime.utcnow() - timedelta(minutes=16)   # 10 idle + 5 to answer
        db.session.commit()
    r = c.get('/', headers={'Accept': 'application/json'})
    assert r.status_code == 401 and 'inactivity' in r.get_json()['message']
