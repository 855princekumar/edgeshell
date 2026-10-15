"""Verified email for every account (when outgoing mail is set up), password
reset by email for every role, and ticket mail over SMTP. Without mail the
platform behaves as before, so nobody is locked out by a missing mail server."""
import re
from datetime import datetime

import pytest

from app import create_app, db, limiter
from app.models.user import PasswordResetRequest, User


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
        u = User(username='op', email='op@fleet.test', role='operator', is_active=True, must_change_password=False)
        u.set_password('Operator-pass-1')
        db.session.add(u)
        db.session.commit()
    yield app


@pytest.fixture
def mail(app, monkeypatch):
    """SMTP on, every message captured instead of sent."""
    app.config.update(SMTP_HOST='smtp.test', SMTP_FROM='es@fleet.test', PUBLIC_URL='https://fleet.test')
    sent = []
    import app.services.email_service as es
    monkeypatch.setattr(es, '_send_smtp', lambda to, subject, text: sent.append((to, subject, text)) or (True, 'sent'))
    return sent


def login(app, user='op', pw='Operator-pass-1'):
    c = app.test_client()
    c.post('/login', data={'username': user, 'password': pw})
    return c


def link_in(msg, path):
    return re.search(r'https://fleet\.test(' + path + r'\S+)', msg[2]).group(1)


def test_without_mail_nothing_changes_for_users(app):
    c = login(app)
    assert c.get('/nodes/').status_code == 200                      # no gate for an operator


def test_with_mail_every_account_verifies_its_email_before_the_app_opens(app, mail):
    c = login(app)
    r = c.get('/nodes/')
    assert r.status_code == 302 and '/account/email' in r.headers['Location']
    c.post('/account/email/resend')                                 # confirm the current address
    assert mail and mail[-1][0] == 'op@fleet.test'
    c.get(link_in(mail[-1], '/account/email/verify/'))
    with app.app_context():
        u = User.query.filter_by(username='op').first()
        assert u.email_verified_at is not None and u.email_pending is None
    assert c.get('/nodes/').status_code == 200


def test_a_new_address_is_used_only_after_its_link_is_opened(app, mail):
    with app.app_context():
        User.query.filter_by(username='op').first().email_verified_at = datetime.utcnow()
        db.session.commit()
    c = login(app)
    c.post('/account/email', data={'email': 'first@mail.test', 'password': 'Operator-pass-1'})
    first = link_in(mail[-1], '/account/email/verify/')
    c.post('/account/email', data={'email': 'second@mail.test', 'password': 'Operator-pass-1'})
    second = link_in(mail[-1], '/account/email/verify/')
    with app.app_context():
        u = User.query.filter_by(username='op').first()
        assert u.email == 'op@fleet.test' and u.email_pending == 'second@mail.test'   # old one still in use
    app.test_client().get(first)                                    # an older link no longer works
    with app.app_context():
        assert User.query.filter_by(username='op').first().email == 'op@fleet.test'
    app.test_client().get(second)                                   # opened on another device, signed out
    with app.app_context():
        u = User.query.filter_by(username='op').first()
        assert u.email == 'second@mail.test' and u.email_verified()
    assert app.test_client().get('/account/email/verify/not-a-token').status_code == 302


def test_every_role_resets_by_email_and_2fa_stays_on(app, mail):
    with app.app_context():
        User.query.filter_by(username='op').first().email_verified_at = datetime.utcnow()
        db.session.commit()
    c = app.test_client()
    c.post('/forgot', data={'username': 'op'})
    assert mail[-1][0] == 'op@fleet.test' and 'password reset' in mail[-1][1].lower()
    link = link_in(mail[-1], '/reset/')
    r = c.post(link, data={'new_password': 'Fresh-operator-9', 'confirm_password': 'Fresh-operator-9'})
    assert r.status_code == 302 and '/login' in r.headers['Location']
    with app.app_context():
        assert User.query.filter_by(username='op').first().check_password('Fresh-operator-9')
        assert PasswordResetRequest.query.count() == 0              # no admin needed
    r = c.post(link, data={'new_password': 'Another-one-77', 'confirm_password': 'Another-one-77'})
    assert '/forgot' in r.headers['Location']                       # single use


def test_an_unverified_account_still_goes_to_the_administrators(app, mail):
    app.test_client().post('/forgot', data={'username': 'op'})
    assert not any(m[0] == 'op@fleet.test' for m in mail)           # never to an unproven address
    with app.app_context():
        assert PasswordResetRequest.query.count() == 1


def test_a_gated_administrator_can_still_fix_the_mail_settings(app, mail):
    c = login(app, 'admin', 'admin123')
    assert '/account/email' in c.get('/nodes/').headers['Location']
    assert c.get('/issues/email-settings').status_code == 200


def test_ticket_mail_goes_over_smtp_when_usesend_is_off(app, mail):
    with app.app_context():
        for u in User.query.all():
            u.email_verified_at = datetime.utcnow()
        db.session.commit()
    c = login(app)
    c.post('/issues/new', data={'subject': 'Node 7 offline', 'body': 'Since 9:00', 'priority': 'normal'})
    to = [m[0] for m in mail]
    assert 'owner@fleet.test' in to and 'op@fleet.test' in to       # administrators, and a receipt
    assert any('New issue: Node 7 offline' in m[1] for m in mail)


def test_administrators_see_waiting_tickets_and_requests_in_the_sidebar(app):
    c = login(app)
    c.post('/issues/new', data={'subject': 'Help', 'body': 'Please', 'priority': 'normal'})
    app.test_client().post('/forgot', data={'username': 'op'})
    page = login(app, 'admin', 'admin123').get('/nodes/').get_data(as_text=True)
    assert 'title="Open tickets">1<' in page
    assert 'title="Password reset requests and sign-ups waiting">1<' in page
    assert 'nav-badge' not in login(app).get('/nodes/').get_data(as_text=True)    # operators: no badges
