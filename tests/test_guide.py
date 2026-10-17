"""The field guide (/guide/) and the top bar: every signed-in role opens the
guide from the (i) button, it is self-contained (no scripts from elsewhere),
its close button only ever returns to an EdgeShell page, and the account
button is a real, labelled button on every page."""
import re

import pytest

from app import create_app, db, limiter
from app.models.user import User

PAGES = ['/dashboard', '/nodes/', '/monitoring/', '/hardware/', '/issues/', '/logs/', '/account/']


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
        db.session.commit()
    yield app


def browser(app, user='admin'):
    c = app.test_client()
    c.post('/login', data={'username': user, 'password': 'admin123' if user == 'admin' else 'Operator-pass-1'})
    return c


def test_the_guide_needs_a_sign_in(app):
    r = app.test_client().get('/guide/')
    assert r.status_code == 302 and '/login' in r.headers['Location']


@pytest.mark.parametrize('user', ['mon', 'op', 'admin'])
def test_every_role_opens_the_guide(app, user):
    r = browser(app, user).get('/guide/')
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and r.mimetype == 'text/html'
    assert "Hi, I'm Nodi" in page and 'Report a hardware issue' in page and 'Confirm replacement' in page
    assert "frame-ancestors 'none'" in r.headers.get('Content-Security-Policy', '')


def test_the_guide_loads_no_scripts_from_elsewhere(app):
    page = browser(app).get('/guide/').get_data(as_text=True)
    assert not re.search(r'<script[^>]+src=', page)                  # all of it is in the page
    hosts = set(re.findall(r'(?:href|src)="https?://([^/"]+)', page))
    assert hosts <= {'fonts.googleapis.com', 'fonts.gstatic.com'}, hosts
    assert '{{' not in page and '{%' not in page                     # nothing left unrendered
    assert '"hardware": "/hardware/"' in page


@pytest.mark.parametrize('ref, want', [
    ('http://localhost/nodes/?x=1', '/nodes/?x=1'),
    ('http://localhost/hardware/', '/hardware/'),
    ('https://evil.example/phish', '/dashboard'),                    # another site
    ('http://localhost//evil.example/x', '/dashboard'),              # protocol-relative trick
    ('http://localhost/guide/', '/dashboard'),                       # itself
    ('javascript:alert(1)', '/dashboard'),
    ('', '/dashboard'),
])
def test_close_returns_only_to_an_edgeshell_page(app, ref, want):
    c = browser(app)
    page = c.get('/guide/', headers={'Referer': ref} if ref else {}).get_data(as_text=True)
    m = re.search(r'id="backBtn" href="([^"]+)"', page)
    assert m and m.group(1).replace('&amp;', '&') == want


@pytest.mark.parametrize('user', ['mon', 'op', 'admin'])
def test_every_page_has_the_guide_and_a_real_account_button(app, user):
    c = browser(app, user)
    for path in PAGES:
        page = c.get(path).get_data(as_text=True)
        assert 'id="topbarGuide" href="/guide/"' in page, path
        btn = re.search(r'<a class="topbar-user" id="topbarAccount" href="/account/" role="button"[^>]*aria-label="My account \(([^)]+)\)"', page)
        assert btn and btn.group(1) == user, path
        assert 'bi-chevron-down' in page and 'href="/guide/" class="nav-item"' in page


def test_the_hardware_page_opens_the_field_guide(app):
    page = browser(app, 'mon').get('/hardware/').get_data(as_text=True)
    assert 'id="hwGuideBtn" href="/guide/#find"' in page
