"""Smoke tests: app boots, health responds, auth gates work, RBAC denies."""
import pytest
from app import create_app, db
from app.models.user import User


@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
        admin = User.query.filter_by(username='admin').first()
        admin.email = 'admin@fleet.test'      # admins must have a real email
        admin.must_change_password = False
        db.session.commit()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _login(app, client, role='administrator'):
    with app.app_context():
        u = User.query.filter_by(username='admin').first()
        u.must_change_password = False
        u.email = 'admin@fleet.test'
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


@pytest.mark.skipif(__import__('sys').platform == 'win32',
                    reason='runs the wrapper in a POSIX shell, as on the nodes; Git Bash on Windows rewrites /tmp')
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
    ca.post('/issues/new', data={'subject': 'alice-only-ticket-subject', 'body': 'B1',
            'category': 'access', 'priority': 'high'}, follow_redirects=True)
    with app.app_context():
        iid = Issue.query.first().id
    assert ca.get(f'/issues/{iid}').status_code == 200
    # bob is isolated
    cb = app.test_client()
    cb.post('/login', data={'username': 'bob', 'password': 'pw'}, follow_redirects=True)
    assert cb.get(f'/issues/{iid}').status_code == 403
    assert b'alice-only-ticket-subject' not in cb.get('/issues/').data
    # admin sees all and can reply + set status
    _login(app, client)  # admin
    assert b'alice-only-ticket-subject' in client.get('/issues/').data
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


def test_fleet_summary_reports_average_temperature(app, client):
    _login(app, client)
    from app.models.node import Node
    with app.app_context():
        for i, t in enumerate((40.0, 60.0)):
            db.session.add(Node(name=f'temp-{i}', ip_address=f'10.9.0.{i + 1}', username='pi',
                                status='online', temperature=t))
        db.session.commit()
    d = client.get('/monitoring/fleet-summary').get_json()
    assert d['avg_temp'] == 50.0


def test_favicon_served(client):
    r = client.get('/favicon.ico')
    assert r.status_code == 301 and r.headers['Location'].endswith('/static/favicon.svg')
    r = client.get('/static/favicon.svg')
    assert r.status_code == 200 and b'<svg' in r.data
    assert b'favicon.svg' in client.get('/login').data


def test_every_rendered_template_is_in_the_repository():
    """A .gitignore rule once hid app/templates/logs/, so those pages worked
    locally and failed with a 500 in the published image. CI runs on a clean
    checkout, so this catches any template that is missing from git."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    names = set()
    for py in (root / 'app').rglob('*.py'):
        names.update(re.findall(r"render_template\(\s*'([^']+)'", py.read_text(encoding='utf-8')))
    for html in (root / 'app' / 'templates').rglob('*.html'):
        names.update(re.findall(r"{%\s*(?:extends|include)\s+\"([^\"]+)\"", html.read_text(encoding='utf-8')))
    missing = sorted(n for n in names if not (root / 'app' / 'templates' / n).is_file())
    assert not missing, f'templates referenced but missing: {missing}'


def test_log_pages_render(app, client):
    _login(app, client)
    for path in ('/logs/', '/logs/activity', '/logs/node/no-such-node'):
        r = client.get(path)
        assert r.status_code in (200, 302, 404), (path, r.status_code)
    assert client.get('/logs/').status_code == 200
    assert client.get('/logs/activity').status_code == 200


def test_page_scripts_parse(app, client):
    """Every inline script on the main pages must be valid JavaScript. A
    stray quote once broke the shared script on every page (CSRF header,
    session watcher, Select all) while every server-side test passed."""
    import re
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js not installed')
    _login(app, client)
    with app.app_context():
        from app.models.node import Node
        db.session.add(Node(name="quote's-test-node", ip_address='10.0.0.1', username='pi'))
        db.session.commit()
    pages = ['/', '/nodes/', '/monitoring/', '/terminal/', '/files/', '/logs/', '/logs/activity',
             '/users/', '/users/add', '/account/', '/tailscale/', '/settings/', '/issues/',
             '/nodes/add', '/nodes/import']
    with tempfile.TemporaryDirectory() as tmp:
        for page in pages:
            r = client.get(page)
            assert r.status_code == 200, (page, r.status_code)
            for i, js in enumerate(re.findall(r'<script>(.*?)</script>', r.get_data(as_text=True), re.S)):
                f = Path(tmp) / f'{page.strip("/").replace("/", "_") or "root"}_{i}.js'
                f.write_text(js, encoding='utf-8')
                res = subprocess.run([node, '--check', str(f)], capture_output=True, text=True)
                assert res.returncode == 0, f'{page} script {i}: {res.stderr.strip()[:400]}'


def test_no_page_defines_a_function_twice(app, client):
    """All inline scripts of a page share one scope, so a second
    `function x()` silently replaces the first. Monitoring once had an old
    pollAll that collected every node live over SSH every 15 seconds, one
    request per node, hidden behind the fast one-request version."""
    import collections
    import re
    _login(app, client)
    pages = ['/', '/nodes/', '/monitoring/', '/terminal/', '/files/', '/logs/', '/logs/activity',
             '/users/', '/users/add', '/account/', '/tailscale/', '/settings/', '/issues/',
             '/nodes/add', '/nodes/import']
    decl = re.compile(r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(')

    def top_level_functions(js):
        """Names declared at brace depth 0 (strings, templates, comments skipped)."""
        out, depth, i, n = [], 0, 0, len(js)
        while i < n:
            c = js[i]
            if js.startswith('//', i):
                i = js.find('\n', i)
                i = n if i < 0 else i
                continue
            if js.startswith('/*', i):
                i = js.find('*/', i + 2)
                i = n if i < 0 else i + 2
                continue
            if c in '\'"`':
                j = i + 1
                while j < n and js[j] != c:
                    j += 2 if js[j] == '\\' else 1
                i = j + 1
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif depth == 0 and js.startswith('function', i):
                m = decl.match(js, i)
                if m:
                    out.append(m.group(1))
            i += 1
        return out

    for page in pages:
        html = client.get(page).get_data(as_text=True)
        names = [f for js in re.findall(r'<script>(.*?)</script>', html, re.S) for f in top_level_functions(js)]
        twice = [n for n, c in collections.Counter(names).items() if c > 1]
        assert not twice, f'{page} defines {twice} more than once'


def test_large_import_stays_out_of_the_cookie_and_checks_in_background(app, client, monkeypatch):
    """Rows used to live in the session cookie: a few hundred nodes overflowed
    the proxy header limit (502). They now wait on the server, and the SSH
    check runs as a background job with progress."""
    import io
    import time
    import app.routes.nodes as nodes_routes
    import app.services.collector as collector

    def fake_collect(t):            # every tenth node answers, the rest refuse the login
        if t['id'] % 10 == 0:
            return {'ok': True, 'metrics': collector.parse_metrics(SAMPLE_PROC), 'kind': None,
                    'message': None, 'seconds': 0.01, 'reconnected': False}
        return {'ok': False, 'metrics': None, 'kind': 'auth', 'message': 'Authentication failed',
                'seconds': 0.01, 'reconnected': True}
    monkeypatch.setattr(collector, 'collect_one', fake_collect)
    monkeypatch.setattr(nodes_routes.orgui_service, 'persist_check', lambda n: None)
    _login(app, client)
    csv = 'name,ip,username,password\n' + '\n'.join(
        f'bulk-node-{i:04d},10.9.{i // 250}.{i % 250 + 1},pi,raspberry' for i in range(400))
    r = client.post('/nodes/import', data={'file': (io.BytesIO(csv.encode()), 'fleet.csv')},
                    content_type='multipart/form-data')
    assert r.status_code == 200 and b'400' in r.data
    cookie = r.headers.get('Set-Cookie', '')
    assert len(cookie) < 1500, f'session cookie is {len(cookie)} bytes'
    with app.app_context():                 # before the job starts (see below)
        uid = User.query.filter_by(username='admin').first().id
    t0 = time.time()
    r = client.post('/nodes/import/commit', data={
        'map__name': 'name', 'map__ip': 'ip_address', 'map__username': 'username',
        'map__password': 'password'})
    assert r.status_code == 302 and time.time() - t0 < 20
    with app.app_context():
        from app.models.node import Node
        assert Node.query.filter(Node.name.like('bulk-node-%')).count() == 400
    # Touch neither the app nor the database while the job runs: the in-memory
    # test database shares one connection between threads, so another
    # session's cleanup could roll back a write the job is making. (MariaDB and
    # file SQLite give each session its own connection.)
    from app.services import jobs as jobs_service
    for _ in range(300):
        jobs = jobs_service.for_user(uid)
        if jobs and jobs[0]['state'] != 'running':
            break
        time.sleep(0.2)
    job = jobs[0]
    time.sleep(0.3)                          # let the last write commit
    assert client.get('/nodes/jobs').get_json()['jobs'][0]['id'] == job['id']
    assert job['state'] == 'done' and job['total'] == 400 and job['ok'] == 40, job
    online = 0
    for _ in range(50):                 # the last write can land just after 'done'
        with app.app_context():
            from app.models.node import Node
            db.session.expire_all()
            online = Node.query.filter_by(status='online').count()
        if online == 40:
            break
        time.sleep(0.1)
    assert online == 40
    r = client.post('/nodes/import/commit', data={})          # token is single use
    assert r.status_code == 302 and '/nodes/import' in r.headers['Location']


# /proc output of a Raspberry Pi as returned by collector.METRICS_CMD
SAMPLE_PROC = """cpu  2255 34 2290 22625563 6290 127 456 0 0 0
cpu  2275 34 2300 22625583 6290 127 456 0 0 0
@@
0.52 0.41 0.30 1/312 4210
@@
MemTotal:        3884328 kB
MemFree:         1834212 kB
MemAvailable:    2911232 kB
@@
/dev/root         30509732 6213352 23005472  22% /
@@
86400.12 345000.40
@@
temp=48.3'C
@@
5032148 1204411
@@
aarch64
@@
docker_installed
docker_running
nodered_installed
nodered_running
orgui_installed
@@
"""


def test_metrics_command_output_is_parsed():
    import app.services.collector as collector
    m = collector.parse_metrics(SAMPLE_PROC)
    assert m['cpu_percent'] == 60.0                    # 30 busy of 50 jiffies
    assert m['mem_percent'] == 25.1 and m['mem_total_mb'] == 3793
    assert m['disk_percent'] == 21.3 and m['disk_total_gb'] == 29.1
    assert (m['load_1'], m['load_15'], m['load_trend']) == (0.52, 0.30, 'rising')
    assert m['uptime'] == '1d 0h 0m' and m['temperature'] == 48.3
    assert (m['net_rx_bytes'], m['net_tx_bytes']) == (5032148, 1204411)
    assert m['arch'] == 'aarch64' and m['docker_running'] and m['nodered_running']
    t = collector.parse_metrics(SAMPLE_PROC.replace("temp=48.3'C", '51234'))
    assert t['temperature'] == 51.2


def test_ssh_failures_are_classified():
    import socket
    import paramiko
    from app.services.collector import classify
    assert classify(paramiko.AuthenticationException('x'))[0] == 'auth'
    assert classify(socket.timeout())[0] == 'unreachable'
    assert classify(ConnectionRefusedError())[0] == 'unreachable'
    assert classify(paramiko.SSHException('banner'))[0] == 'error'


def test_auth_failure_shows_on_the_card_and_credentials_fix_it(app, client, monkeypatch):
    import app.services.collector as collector
    _login(app, client)
    with app.app_context():
        from app.models.node import Node
        n = Node(name='plant-gateway', ip_address='100.64.0.9', username='pi', status='unknown')
        n.password = 'wrong'
        db.session.add(n)
        db.session.commit()
        nid = n.id
    seen = []

    def fake_collect(t):
        seen.append((t['username'], t['password'], t['port']))
        if t['password'] == 'right':
            return {'ok': True, 'metrics': collector.parse_metrics(SAMPLE_PROC), 'kind': None,
                    'message': None, 'seconds': 0.3, 'reconnected': True}
        return {'ok': False, 'metrics': None, 'kind': 'auth', 'seconds': 0.9, 'reconnected': True,
                'message': 'Authentication failed: check the SSH username and password'}
    monkeypatch.setattr(collector, 'collect_one', fake_collect)

    r = client.post(f'/nodes/{nid}/test').get_json()
    assert not r['success'] and r['kind'] == 'auth' and r['status'] == 'auth_failed'
    summary = client.get('/monitoring/fleet-summary').get_json()
    card = [x for x in summary['nodes'] if x['id'] == nid][0]
    assert card['status'] == 'auth_failed' and 'Authentication failed' in card['last_error']
    assert summary['auth_failed'] == 1
    cached = client.post('/monitoring/poll', json={'node_ids': [nid], 'cached': True}).get_json()
    assert cached['results'][str(nid)]['kind'] == 'auth'

    r = client.post(f'/nodes/{nid}/credentials', json={'username': 'admin-pi', 'password': 'right',
                                                       'ssh_port': 2222}).get_json()
    assert r['success'] and r['status'] == 'online'
    assert seen[-1] == ('admin-pi', 'right', 2222)
    with app.app_context():
        from app.models.node import Node
        n = db.session.get(Node, nid)
        assert n.status == 'online' and n.last_error is None and n.cpu_percent == 60.0
    live = client.post('/monitoring/poll', json={'node_ids': [nid]}).get_json()
    assert live['results'][str(nid)]['success'] is True


def test_failing_nodes_back_off_instead_of_retrying_every_cycle(app, monkeypatch):
    import app.services.collector as collector
    calls = []
    monkeypatch.setattr(collector, 'collect_one', lambda t: calls.append(t['id']) or {
        'ok': False, 'metrics': None, 'kind': 'auth', 'message': 'no', 'seconds': 0, 'reconnected': True})
    c = collector.Collector()
    with app.app_context():
        from app.models.node import Node
        db.session.add(Node(name='locked-out', ip_address='10.0.0.2', username='pi'))
        db.session.commit()
        c.run_cycle()
        c.run_cycle()                                   # within the 10 min auth retry window
        assert len(calls) == 1
        c.kick()
        c.run_cycle()                                   # credentials changed: retry now
        assert len(calls) == 2


def test_fleet_poll_runs_in_parallel(app, monkeypatch):
    """60 nodes that each take 0.3 s (one SSH round trip) finish in about one
    round trip, not 18 s."""
    import time
    import app.services.collector as collector

    def slow(t):
        time.sleep(0.3)
        return {'ok': True, 'metrics': collector.parse_metrics(SAMPLE_PROC), 'kind': None,
                'message': None, 'seconds': 0.3, 'reconnected': False}
    monkeypatch.setattr(collector, 'collect_one', slow)
    with app.app_context():
        from app.models.node import Node
        for i in range(60):
            db.session.add(Node(name=f'par-{i}', ip_address=f'10.1.0.{i + 1}', username='pi'))
        db.session.commit()
        t0 = time.time()
        res = collector.Collector().run_cycle()
        took = time.time() - t0
    assert len(res) == 60 and all(r['ok'] for r in res.values())
    assert took < 3, f'fleet pass took {took:.1f}s'



def _fleet(app):
    from app.models.node import Node
    with app.app_context():
        for name, t, st in (('pi-1', 'pi-node', 'online'), ('pi-2', 'pi-node', 'auth_failed'),
                            ('gw-1', 'gateway', 'online'), ('pc-1', 'mini-pc', 'offline'),
                            ('rtu-1', 'rtu', 'online')):
            db.session.add(Node(name=name, ip_address='10.5.0.1', username='pi', node_type=t,
                                status=st, cpu_percent=10.0 if st == 'online' else None,
                                tags='pi-node;ts-online;tailscale'))
        db.session.commit()


def test_default_categories_group_nodes_by_type(app, client):
    _login(app, client)
    _fleet(app)
    d = client.get('/monitoring/fleet-summary').get_json()
    names = [c['name'] for c in d['categories']]
    assert names == ['Pi Nodes', 'Edge Gateways', 'Mini PCs']
    by_type = {n['name']: n['node_type'] for n in d['nodes']}
    assert by_type['gw-1'] == 'gateway' and by_type['pc-1'] == 'mini-pc'
    info = client.get('/monitoring/categories').get_json()
    assert info['types'] == {'mini-pc': 1, 'pi-node': 2, 'gateway': 1, 'rtu': 1}


def test_admin_edits_categories_and_others_cannot(app, client):
    _login(app, client)
    new = [{'name': 'Pi Nodes', 'types': ['pi-node'], 'icon': 'hdd-network', 'sla_target': 99.5},
           {'name': 'RTUs', 'types': 'rtu', 'icon': 'router', 'sla_target': 95}]
    r = client.post('/monitoring/categories', json={'categories': new}).get_json()
    assert r['success'] and r['categories'][1] == {'name': 'RTUs', 'types': ['rtu'], 'icon': 'router',
                                                  'sla_target': 95.0}
    assert [c['name'] for c in client.get('/monitoring/fleet-summary').get_json()['categories']] == \
        ['Pi Nodes', 'RTUs']
    bad = client.post('/monitoring/categories', json={'categories': [
        {'name': 'A', 'types': ['rtu']}, {'name': 'B', 'types': ['rtu']}]})
    assert bad.status_code == 400 and 'both' in bad.get_json()['message']
    assert client.post('/monitoring/categories', json={'categories': [{'name': '', 'types': ['x']}]}).status_code == 400
    _mk_user(app, 'opsy', role='operator')
    op = app.test_client()
    op.post('/login', data={'username': 'opsy', 'password': 'pw'})
    assert op.post('/monitoring/categories', json={'categories': new}).status_code == 403
    assert op.get('/monitoring/categories').status_code == 200


def test_node_fleet_category_filter_and_tags(app, client):
    _login(app, client)
    _fleet(app)
    html = client.get('/nodes/').get_data(as_text=True)
    assert 'id="catFilter"' in html and '<option value="Edge Gateways">' in html
    assert '<option value="Other">Other</option>' in html            # rtu is in no category
    assert 'data-category="Mini PCs"' in html
    assert '>ts-online</span>' in html and '>tailscale</span>' in html  # ; separated tags split
    assert 'value="auth_failed">login failed' in html



def test_orgui_detection_ignores_raspberry_pi_vnc():
    """Raspberry Pi OS runs wayvnc / rpi-connectd on every node with VNC on;
    that is not OpenRemoteGUI. Only the install plus a listening gateway counts,
    and no check may use a pgrep pattern that matches its own command line."""
    from app.services import orgui_service, capability_service, collector
    for cmd in (orgui_service._CHECK, capability_service._check_cmd(6080), collector.METRICS_CMD):
        assert 'wayvnc' not in cmd and 'websockify' not in cmd
        assert 'pgrep -f' not in cmd, 'pgrep -f can match the checking shell itself'
    assert '/opt/openremotegui' in orgui_service._CHECK
    m = collector.parse_metrics(SAMPLE_PROC)
    assert m['orgui_installed'] is True and m['orgui_running'] is False   # installed, gateway down


def test_capability_probe_parses_orgui_line(app, monkeypatch):
    from app.services import capability_service
    from app.models.node import Node
    outputs = iter(['\n'.join(['ARCH:aarch64', 'DOCKER:0', 'NODERED:1', 'ORGUI:0']),
                    '\n'.join(['ARCH:aarch64', 'DOCKER:1', 'NODERED:1', 'ORGUI:1'])])
    monkeypatch.setattr(capability_service, 'execute_command',
                        lambda node, cmd, timeout=20: {'success': True, 'stdout': next(outputs)})
    with app.app_context():
        n = Node(name='cap', ip_address='10.0.0.3', username='pi')
        assert capability_service.check_node(n)['orgui'] is False
        assert capability_service.check_node(n)['orgui'] is True



def test_stale_orgui_flags_are_reset_once(app):
    from app import _one_time_fixes
    from app.models.node import Node
    from app.models import Settings
    with app.app_context():
        Settings.query.filter_by(key='fix_orgui_detection_v2').delete()
        db.session.add(Node(name='stale', ip_address='10.0.0.4', username='pi', orgui_installed=True))
        db.session.commit()
        _one_time_fixes()
        assert db.session.query(Node.orgui_installed).filter_by(name='stale').scalar() is None
        n = Node.query.filter_by(name='stale').first()
        n.orgui_installed = True                       # detected again later: kept
        db.session.commit()
        _one_time_fixes()
        assert db.session.query(Node.orgui_installed).filter_by(name='stale').scalar() is True
