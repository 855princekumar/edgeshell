"""Fleet terminal streaming, sudo with a password, the guided OpenRemoteGUI
install and the interactive shell's origin check. SSH is simulated; the
live behaviour is covered by sandbox/run.sh against emulated nodes."""
import json
import time

import pytest

from app import create_app, db
from app.models.node import Node
from app.models.user import User


@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        db.drop_all()
        db.create_all()
        from app import _seed_defaults
        _seed_defaults()
        a = User.query.filter_by(username='admin').first()
        a.must_change_password = False
        a.email = 'o@fleet.test'
        for name, status in (('pi-a', 'online'), ('pi-b', 'online'), ('pi-down', 'offline'),
                             ('pi-badpw', 'auth_failed')):
            n = Node(name=name, ip_address='10.0.0.1', username='pi', status=status)
            n.password = 'secret-pw'
            db.session.add(n)
        db.session.commit()
    yield app


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    return c


def _ids(app):
    with app.app_context():
        return {n.name: n.id for n in Node.query.all()}


def test_sudo_password_goes_over_stdin_never_the_command_line(monkeypatch):
    import app.services.collector as collector
    sent = {}

    class Chan:
        def __init__(self):
            self.done = False
        def get_pty(self, **k): sent['pty'] = True
        def set_combine_stderr(self, v): pass
        def exec_command(self, cmd): sent['cmd'] = cmd
        def sendall(self, data): sent['stdin'] = data
        def recv_ready(self): return not self.done
        def recv(self, n):
            self.done = True
            return b'root\n'
        def exit_status_ready(self): return self.done
        def recv_exit_status(self): return 0
        def close(self): pass

    class Transport:
        def open_session(self, timeout=None): return Chan()

    class Client:
        def get_transport(self): return Transport()

    monkeypatch.setattr(collector.POOL, 'get', lambda t: (Client(), False))
    r = collector.run_command({'id': 1}, 'sudo whoami', sudo_password='p@ss w0rd')
    assert r['success'] and r['stdout'] == 'root\n'
    assert 'p@ss w0rd' not in sent['cmd']                       # not on the command line
    assert sent['stdin'] == b'p@ss w0rd\n' and 'pty' not in sent  # stdin, no echoing terminal
    assert 'command sudo -S' in sent['cmd']
    sent.clear()
    collector.run_command({'id': 1}, 'uptime', sudo_password='x')
    assert 'stdin' not in sent and sent.get('pty')              # no sudo: untouched, with a PTY


def test_terminal_streams_results_and_skips_unreachable_nodes(app, client, monkeypatch):
    import app.services.collector as collector
    calls = []

    def fake_run(t, command, timeout=60, sudo_password=None, **k):
        calls.append(t['name'])
        time.sleep(0.2 if t['name'] == 'pi-a' else 0.01)
        return {'stdout': f"hi from {t['name']}\n__EDGESHELL_CWD__/home/pi", 'exit_code': 0,
                'success': True, 'duration': 0.2, 'error': None, 'kind': None}
    monkeypatch.setattr(collector, 'run_command', fake_run)
    ids = _ids(app)
    r = client.post('/terminal/execute', json={'command': 'echo hi', 'node_ids': list(ids.values())})
    assert r.mimetype == 'application/x-ndjson'
    lines = [json.loads(x) for x in r.get_data(as_text=True).splitlines() if x.strip()]
    assert lines[0]['type'] == 'start' and lines[-1]['type'] == 'done'
    assert {s['name']: s['reason'] for s in lines[0]['skipped']} == {'pi-down': 'offline',
                                                                     'pi-badpw': 'login failed'}
    results = [x for x in lines if x['type'] == 'result']
    assert [x['node'] for x in results] == ['pi-b', 'pi-a']    # each as soon as it finishes
    assert results[0]['stdout'] == 'hi from pi-b' and results[0]['cwd'] == '/home/pi'
    assert sorted(calls) == ['pi-a', 'pi-b']                   # unreachable nodes never tried


def test_monitor_cannot_use_the_terminal(app):
    with app.app_context():
        u = User(username='mon', email='mon@fleet.test', role='monitor', is_active=True,
                 must_change_password=False)
        u.set_password('Monitor-pass-1')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    c.post('/login', data={'username': 'mon', 'password': 'Monitor-pass-1'})
    assert c.post('/terminal/execute', json={'command': 'id', 'node_ids': [1]}).status_code == 403


class FakeNode:
    """Scripted node for the install task: answers by command."""

    def __init__(self, gui=True, install_ok=True, running_after=True, free_mb=4000):
        self.gui, self.install_ok, self.running_after, self.free_mb = gui, install_ok, running_after, free_mb
        self.commands = []

    def run(self, t, command, timeout=60, sudo_password=None, on_output=None, get_pty=True, stop=None):
        self.commands.append(command)
        ok = {'exit_code': 0, 'success': True, 'duration': 0.1, 'error': None, 'kind': None}
        if command.startswith('echo "OS:'):
            return dict(ok, stdout=f'OS:raspbian 12\nFREE_MB:{self.free_mb}\nGIT:1\nNET:1\nGUI:{int(self.gui)}\n')
        if command == 'sudo true':
            return dict(ok, stdout='')
        if 'git clone' in command:
            if on_output:
                on_output("Cloning into 'OpenRemoteGUI'...")
            return dict(ok, stdout='cloned')
        if 'install.sh' in command:
            if on_output:
                on_output('installing noVNC...')
            return dict(ok, stdout='done') if self.install_ok else dict(ok, success=False, exit_code=1, stdout='boom')
        if command.startswith('d=0;'):
            return dict(ok, stdout='RUNNING\n' if self.running_after else 'STOPPED\n')
        return dict(ok, stdout='')


def _run_install(app, client, monkeypatch, fake, url):
    import app.services.collector as collector
    from app.services import jobs
    import urllib.request

    class Page:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(collector, 'run_command', fake.run)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: Page())
    r = client.post(url).get_json()
    with app.app_context():
        uid = User.query.filter_by(username='admin').first().id
    for _ in range(100):
        j = jobs.get(r['job_id'], uid)
        if j['state'] != 'running':
            return j
        time.sleep(0.05)
    return j


def test_guided_install_runs_each_step_in_order(app, client, monkeypatch):
    fake = FakeNode()
    nid = _ids(app)['pi-a']
    j = _run_install(app, client, monkeypatch, fake, f'/nodes/{nid}/orgui/install')
    assert j['state'] == 'done' and j['ok'] == 1, j
    order = [c for c in fake.commands if 'git clone' in c or 'install.sh' in c]
    assert 'git clone --depth 1 https://github.com/855princekumar/OpenRemoteGUI.git' in order[0]
    assert order[1] == 'cd "$HOME/OpenRemoteGUI" && sudo bash install.sh'
    assert any('installing noVNC' in line for line in j['log'])      # live output reaches the log
    with app.app_context():
        assert db.session.get(Node, nid).orgui_installed is True


def test_install_refuses_unsafe_nodes_without_touching_them(app, client, monkeypatch):
    fake = FakeNode(gui=False, free_mb=100)
    nid = _ids(app)['pi-a']
    j = _run_install(app, client, monkeypatch, fake, f'/nodes/{nid}/orgui/install')
    item = j['items'][0]
    assert item['state'] == 'skipped' and 'desktop' in item['message'] and 'MB free' in item['message']
    assert not any('git clone' in c or 'install.sh' in c for c in fake.commands)


def test_fleet_install_stops_at_the_first_failure(app, client, monkeypatch):
    fake = FakeNode(install_ok=False)
    j = _run_install(app, client, monkeypatch, fake, '/nodes/orgui/install-all')
    states = [i['state'] for i in j['items']]
    assert states == ['failed', 'skipped'], j['items']           # pi-a failed, pi-b not started
    assert sum('install.sh' in c for c in fake.commands) == 1


def test_shell_origin_check(app):
    from app.routes.shell import _same_origin
    with app.test_request_context('/ws/nodes/1/shell', headers={'Origin': 'https://fleet.test', 'Host': 'fleet.test'}):
        assert _same_origin()
    with app.test_request_context('/ws/nodes/1/shell', headers={'Origin': 'https://evil.test', 'Host': 'fleet.test'}):
        assert not _same_origin()
    with app.test_request_context('/ws/nodes/1/shell', headers={'Origin': 'http://10.0.0.5:8080', 'Host': '10.0.0.5'}):
        assert _same_origin()


def _fake_ssh(monkeypatch, banner, exc):
    """paramiko.SSHClient whose connect fails like a node would; records close()."""
    import paramiko
    closed = []

    class T:
        remote_version = banner

    def connect(self, **k):
        self._transport = T()
        raise exc

    monkeypatch.setattr(paramiko.SSHClient, 'connect', connect)
    monkeypatch.setattr(paramiko.SSHClient, 'get_transport', lambda self: getattr(self, '_transport', None))
    monkeypatch.setattr(paramiko.SSHClient, 'close', lambda self: closed.append(1))
    return closed


@pytest.mark.parametrize('exc', [__import__('paramiko').AuthenticationException('bad'), TimeoutError('slow'),
                                 ConnectionRefusedError()])
def test_failed_logins_close_their_connection(monkeypatch, exc):
    # a half-open client left behind keeps a socket and a thread: hundreds an
    # hour with a few wrong passwords, until the server runs out of file handles
    from app.services import ssh_service
    closed = _fake_ssh(monkeypatch, 'SSH-2.0-OpenSSH_9.2p1', exc)
    with pytest.raises(type(exc)):
        ssh_service.connect('10.0.0.1', 22, 'pi', 'pw')
    assert closed == [1]


def test_tailscale_ssh_refusal_is_reported_as_such(monkeypatch):
    import paramiko
    from app.services import collector, ssh_service
    closed = _fake_ssh(monkeypatch, 'SSH-2.0-Tailscale', paramiko.AuthenticationException('Authentication timeout.'))
    with pytest.raises(ssh_service.TailscaleCheckRequired) as e:
        ssh_service.connect('100.64.0.9', 22, 'pi', 'pw')
    assert closed == [1]
    kind, message = collector.classify(e.value)
    assert kind == 'auth' and 'Tailscale SSH' in message and 'password' in message
    r = collector.collect_one({'id': 999, 'ip': '100.64.0.9', 'port': 22, 'username': 'pi', 'password': 'pw'})
    assert r['kind'] == 'auth' and 'Tailscale SSH' in r['message']
