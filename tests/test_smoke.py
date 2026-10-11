"""Smoke tests: app boots, health responds, auth gates work, RBAC denies."""
import pytest
from app import create_app, db
from app.models.user import User


@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _login(app, client, role='administrator'):
    with app.app_context():
        u = User.query.filter_by(username='admin').first()
        u.must_change_password = False
        u.role = role
        db.session.commit()
    return client.post('/login', data={'username': 'admin', 'password': 'admin123'},
                       follow_redirects=True)


def test_health(client):
    r = client.get('/healthz')
    assert r.status_code in (200, 503)
    assert 'checks' in r.get_json()


def test_login_required(client):
    r = client.get('/nodes/', follow_redirects=False)
    assert r.status_code in (301, 302)


def test_pages_render(app, client):
    _login(app, client)
    for p in ['/', '/nodes/', '/nodes/add', '/nodes/import', '/monitoring/',
              '/users/', '/settings/']:
        assert client.get(p).status_code == 200, p


def test_rbac_monitor_denied_add(app, client):
    _login(app, client, role='monitor')
    r = client.get('/nodes/add', follow_redirects=False)
    assert r.status_code in (302, 403)


def test_import_verify_renders(app, client):
    import io
    _login(app, client)
    data = {'file': (io.BytesIO(b'ip,name,user\n10.0.0.21,pi-a,pi\n'), 'n.csv')}
    r = client.post('/nodes/import', data=data, content_type='multipart/form-data')
    assert r.status_code == 200
    assert b'Confirm and Create' in r.data


def test_db_export_import_roundtrip(app):
    from app.services.dbio_service import export_snapshot, import_snapshot
    from app.models.node import Node
    import tempfile, os
    with app.app_context():
        n = Node(name='rt', ip_address='10.0.0.9', username='pi', status='unknown')
        n.password = 'secret'
        db.session.add(n); db.session.commit()
        fd, path = tempfile.mkstemp(suffix='.json'); os.close(fd)
        export_snapshot(path)
        Node.query.filter_by(name='rt').delete(); db.session.commit()
        import_snapshot(path, wipe=False)
        got = Node.query.filter_by(name='rt').first()
        assert got is not None and got.password == 'secret'


def test_soft_delete_and_restore(app, client):
    from app.models.node import Node
    _login(app, client)
    with app.app_context():
        n = Node(name='sd1', ip_address='10.0.0.7', username='pi', status='online')
        n.password = 'x'; db.session.add(n); db.session.commit(); nid = n.id
    # delete -> recycle bin
    client.post(f'/nodes/{nid}/delete')
    with app.app_context():
        n = db.session.get(Node, nid)
        assert n.is_deleted is True and n.is_active is False
        assert Node.query.filter(Node.is_deleted.isnot(True)).count() == 0
    # restore
    client.post(f'/nodes/{nid}/restore')
    with app.app_context():
        n = db.session.get(Node, nid)
        assert n.is_deleted is False and n.is_active is True


def test_cd_persistence_wrapper(app):
    import subprocess, shlex
    from app.services.ssh_service import _CWD_MARKER

    def run_in_dir(command, cwd=None):
        prefix = f'cd {shlex.quote(cwd)} 2>/dev/null; ' if cwd else ''
        wrapped = (f'{prefix}{command}\n__ec=$?\n'
                   f'printf "%s%s\\n" "{_CWD_MARKER}" "$(pwd)"\nexit $__ec')
        r = subprocess.run(['bash', '-c', wrapped], capture_output=True, text=True)
        new_cwd = cwd
        for line in r.stdout.splitlines():
            if line.startswith(_CWD_MARKER):
                new_cwd = line[len(_CWD_MARKER):].strip() or new_cwd
        return new_cwd

    cwd = run_in_dir('pwd', None)
    cwd = run_in_dir('cd /tmp', cwd)
    assert cwd == '/tmp'
    cwd = run_in_dir('mkdir -p sub && cd sub', cwd)
    assert cwd.endswith('/sub')


def test_multifile_upload_validation(app, client):
    _login(app, client)
    # no files -> 400
    r = client.post('/files/upload', data={'node_ids': '1'},
                    content_type='multipart/form-data')
    assert r.status_code == 400


def _mkuser(app, name, role='operator'):
    from app.models.user import User
    with app.app_context():
        if not User.query.filter_by(username=name).first():
            u = User(username=name, email=f'{name}@x.com', role=role,
                     is_active=True, must_change_password=False)
            u.set_password('pw'); db.session.add(u); db.session.commit()


def test_issue_isolation(app, client):
    from app.models.issue import Issue
    _mkuser(app, 'alice'); _mkuser(app, 'bob')
    ca = app.test_client(); ca.post('/login', data={'username': 'alice', 'password': 'pw'})
    ca.post('/issues/new', data={'subject': 'Secret', 'body': 'hidden', 'priority': 'normal'})
    with app.app_context():
        iid = Issue.query.filter_by(subject='Secret').first().id
    # bob cannot see or open alice's issue
    cb = app.test_client(); cb.post('/login', data={'username': 'bob', 'password': 'pw'})
    assert b'Secret' not in cb.get('/issues/').data
    assert cb.get(f'/issues/{iid}').status_code == 403
    # admin sees all
    _login(app, client)  # admin
    assert client.get(f'/issues/{iid}').status_code == 200


def test_issue_reply_thread(app, client):
    from app.models.issue import Issue
    _mkuser(app, 'carol')
    cc = app.test_client(); cc.post('/login', data={'username': 'carol', 'password': 'pw'})
    cc.post('/issues/new', data={'subject': 'Thread', 'body': 'first', 'priority': 'low'})
    with app.app_context():
        iid = Issue.query.filter_by(subject='Thread').first().id
    cc.post(f'/issues/{iid}/reply', data={'body': 'second'})
    with app.app_context():
        assert len(db.session.get(Issue, iid).messages) == 2


def test_email_disabled_noop(app):
    with app.app_context():
        from app.services.email_service import send_email
        ok, _ = send_email('a@x.com', 's', '<p>b</p>')
        assert ok is False  # disabled by default, never raises


def _mk_user(app, name, role='operator'):
    from app.models.user import User
    with app.app_context():
        if not User.query.filter_by(username=name).first():
            u = User(username=name, email=f'{name}@x.com', role=role,
                     is_active=True, must_change_password=False)
            u.set_password('pw'); db.session.add(u); db.session.commit()


def test_issue_isolation_and_thread(app, client):
    from app.models.issue import Issue
    _mk_user(app, 'alice'); _mk_user(app, 'bob')
    # alice reports
    ca = app.test_client()
    ca.post('/login', data={'username': 'alice', 'password': 'pw'}, follow_redirects=True)
    ca.post('/issues/new', data={'subject': 'S1', 'body': 'B1',
            'category': 'access', 'priority': 'high'}, follow_redirects=True)
    with app.app_context():
        iid = Issue.query.first().id
    assert ca.get(f'/issues/{iid}').status_code == 200
    # bob is isolated
    cb = app.test_client()
    cb.post('/login', data={'username': 'bob', 'password': 'pw'}, follow_redirects=True)
    assert cb.get(f'/issues/{iid}').status_code == 403
    assert b'S1' not in cb.get('/issues/').data
    # admin sees all and can reply + set status
    _login(app, client)  # admin
    assert b'S1' in client.get('/issues/').data
    client.post(f'/issues/{iid}/reply', data={'body': 'on it'}, follow_redirects=True)
    client.post(f'/issues/{iid}/status', data={'status': 'resolved'}, follow_redirects=True)
    with app.app_context():
        assert Issue.query.get(iid).status == 'resolved'


def test_usesend_payload_shape(app, monkeypatch):
    import app.services.email_service as es
    captured = {}

    class R:
        status_code = 200; text = 'ok'

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers); return R()

    monkeypatch.setattr(es.requests, 'post', fake_post)
    app.config.update(EMAIL_ENABLED=True, USESEND_API_KEY='us_k',
                      USESEND_FROM='E <e@x.com>', USESEND_BASE_URL='https://app.usesend.com')
    with app.app_context():
        ok, _ = es.send_email(['a@x.com', 'b@x.com'], 'Sub', '<p>hi</p>')
    assert ok
    assert captured['url'].endswith('/api/v1/emails')
    assert isinstance(captured['json']['to'], list)
    assert captured['headers']['Authorization'] == 'Bearer us_k'


def test_three_tier_isolation(app):
    """monitor read-only, operator ops-but-not-admin, admin full."""
    _mk_user(app, 'mon', role='monitor')
    _mk_user(app, 'op', role='operator')

    def cli(u, pw='pw'):
        c = app.test_client()
        c.post('/login', data={'username': u, 'password': pw}, follow_redirects=True)
        return c

    mon, op, admin = cli('mon'), cli('op'), cli('admin', 'admin123')

    # monitor is redirected/blocked from action + admin areas
    for path in ['/terminal/', '/files/', '/settings/', '/users/', '/issues/email-settings']:
        assert mon.get(path, follow_redirects=False).status_code in (302, 403), path

    # operator gets ops pages but not admin-only areas
    assert op.get('/terminal/', follow_redirects=False).status_code == 200
    assert op.get('/files/', follow_redirects=False).status_code == 200
    for path in ['/settings/', '/users/', '/issues/email-settings']:
        assert op.get(path, follow_redirects=False).status_code in (302, 403), path

    # admin gets everything
    for path in ['/terminal/', '/files/', '/settings/', '/users/', '/issues/email-settings']:
        assert admin.get(path, follow_redirects=False).status_code == 200, path


def test_email_config_saved_encrypted(app, client):
    from app.models import Settings
    _login(app, client)
    client.post('/issues/email-settings', data={
        'email_enabled': 'on', 'usesend_base_url': 'https://app.usesend.com',
        'usesend_from': 'E <e@x.com>', 'usesend_api_key': 'us_secret123'},
        follow_redirects=True)
    with app.app_context():
        from app.services.email_service import get_email_config
        cfg = get_email_config()
        assert cfg['enabled'] and cfg['api_key'] == 'us_secret123'
        raw = Settings.query.filter_by(key='usesend_api_key_enc').first().value
        assert raw != 'us_secret123'  # stored encrypted


def test_capability_routes_gated(app):
    _mk_user(app, 'mon', role='monitor')
    _mk_user(app, 'op', role='operator')

    def cli(u, pw='pw'):
        c = app.test_client()
        c.post('/login', data={'username': u, 'password': pw}, follow_redirects=True)
        return c

    mon, op = cli('mon'), cli('op')
    assert mon.post('/nodes/capabilities/scan', follow_redirects=False).status_code in (302, 403)
    assert op.post('/nodes/capabilities/scan', follow_redirects=False).status_code == 200
    # monitor does not see the fleet setup control
    assert b'data-bs-target="#fleetSetup"' not in mon.get('/nodes/').data
    assert b'data-bs-target="#fleetSetup"' in op.get('/nodes/').data


def test_legacy_capital_role_still_operator(app):
    from app.models.user import User
    with app.app_context():
        u = User(username='LegacyOp', email='lo@x.com', role='Operator',
                 is_active=True, must_change_password=False)
        u.set_password('pw'); db.session.add(u); db.session.commit()
        assert u.can_execute() is True   # case-insensitive


def test_add_user_requires_unique_username_and_email(app, client):
    from app.models.user import User
    _login(app, client)  # admin

    def add(username, email, pw='password123'):
        return client.post('/users/add', data={
            'username': username, 'email': email, 'role': 'monitor',
            'password': pw}, follow_redirects=True)

    # email is mandatory
    r = add('noemail', '')
    assert r.status_code == 200 and b'Email is required' in r.data

    # first create succeeds
    assert add('alice', 'Alice@X.com').status_code == 200
    with app.app_context():
        assert User.query.filter_by(username='alice').first().email == 'alice@x.com'  # lowercased

    # duplicate username (any case) rejected with a friendly message, no 500
    r = add('ALICE', 'other@x.com')
    assert r.status_code == 200 and b'already taken' in r.data
    # duplicate email (any case) rejected
    r = add('bob', 'ALICE@x.com')
    assert r.status_code == 200 and b'already registered' in r.data


def test_username_email_availability_endpoint(app, client):
    _login(app, client)
    client.post('/users/add', data={'username': 'taken', 'email': 'taken@x.com',
                'role': 'monitor', 'password': 'password123'}, follow_redirects=True)
    r = client.get('/users/check?username=taken&email=taken@x.com')
    d = r.get_json()
    assert d['username_taken'] is True and d['email_taken'] is True
    r = client.get('/users/check?username=free&email=free@x.com')
    d = r.get_json()
    assert d['username_taken'] is False and d['email_taken'] is False


def test_import_accepts_emulator_json_and_hostnames():
    from app.services import import_service as imp
    export = ('{"edgeshell_export": true, "node_count": 2, "nodes": ['
              '{"name": "Bookworm64", "ip_address": "pi-emu-bookworm64", "ssh_port": 22,'
              ' "username": "pi", "description": "Pi Fleet Emulator (pi-emu-bookworm64) - 64bit",'
              ' "tags": "simulated,64bit"},'
              '{"name": "Buster32", "ip_address": "host.docker.internal", "ssh_port": 3001,'
              ' "username": "pi", "description": "d", "tags": "t"}]}')
    mapping, rows, headers = imp.parse(export)
    assert mapping['ip_address'] == 'ip_address' and mapping['description'] == 'description'
    nodes, errors = imp.to_nodes(rows, mapping, default_password='raspberry',
                                 host_override='127.0.0.1')
    assert not errors and len(nodes) == 2
    assert nodes[0]['ip_address'] == 'pi-emu-bookworm64' and nodes[0]['password'] == 'raspberry'
    assert nodes[1]['ip_address'] == '127.0.0.1' and nodes[1]['ssh_port'] == 3001


def test_valid_host():
    from app.services.import_service import valid_host
    for ok in ('10.0.0.5', 'pi-emu-node-1', 'host.docker.internal', 'pi.local'):
        assert valid_host(ok), ok
    for bad in ('', '999.1.1.1', 'bad host', '-lead', 'a;rm -rf', '1.2.3'):
        assert not valid_host(bad), bad


def test_csv_import_still_works():
    from app.services import import_service as imp
    mapping, rows, _ = imp.parse('ip,name,user,password\n10.0.0.21,pi-1,pi,raspberry\n')
    nodes, errors = imp.to_nodes(rows, mapping)
    assert not errors and nodes[0]['name'] == 'pi-1' and nodes[0]['password'] == 'raspberry'


def test_ansible_inventory_names_are_safe():
    from types import SimpleNamespace
    from app.services import ansible_service as a
    n = SimpleNamespace(id=7, name='Node 1 (lab)', ip_address='pi-emu-node-1', ssh_port=22,
                        username='pi', password='raspberry')
    host = next(iter(a.build_inventory([n])['all']['hosts']))
    assert ' ' not in host and '(' not in host and host.startswith('n7_')
