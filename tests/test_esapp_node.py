"""esapp's node logic against a simulated systemd and clock (POSIX only: it
uses symlinks): install, the atomic switch, the automatic way back, the
one-click rollback, cleanup that keeps the newest stable versions, and a
removal that leaves nothing. The same flows run for real in tests/esapp_e2e.py."""
import json
import os
import sys
import time as _time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app', 'agents', 'esapp'))
import esapp  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='node logic uses POSIX symlinks')


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s

    strftime = staticmethod(_time.strftime)


class FakeNode:
    """systemd and the user commands, as far as esapp sees them."""

    def __init__(self, root):
        self.root = root
        self.users, self.calls = set(), []
        self.active, self.restarts, self.enabled = 'inactive', 0, False
        self.bad = set()                       # versions that crash after a start

    def current(self):
        link = os.path.join(self.root, 'apps', 'demo-sensor', 'current')
        return os.path.basename(os.readlink(link)) if os.path.islink(link) else None

    def sh(self, args, check=True, timeout=600, env=None):
        self.calls.append(args)
        rc = 0
        if args[0] == 'systemctl':
            cmd = args[1]
            if cmd in ('restart', 'start'):
                self.active = 'failed' if self.current() in self.bad else 'active'
            elif cmd == 'disable':
                self.enabled, self.active = False, 'inactive'
            elif cmd == 'enable':
                self.enabled = True
            elif cmd == 'show':
                return Result(0, 'ActiveState=%s\nNRestarts=%d\n' % (self.active, self.restarts))
        elif args[0] == 'id':
            rc = 0 if args[-1] in self.users else 1
        elif args[0] == 'useradd':
            self.users.add(args[-1])
        elif args[0] == 'userdel':
            self.users.discard(args[-1])
        elif args[0] == 'getent':
            rc = 0 if args[-1] == 'dialout' else 2
        return Result(rc, '')


class Result:
    def __init__(self, rc, out):
        self.returncode, self.stdout = rc, out


@pytest.fixture
def node(tmp_path, monkeypatch):
    root = tmp_path / 'opt-edgeshell'
    units = tmp_path / 'units'
    units.mkdir()
    monkeypatch.setenv('ESAPP_ROOT', str(root))
    monkeypatch.setattr(esapp, 'ROOT', str(root))
    monkeypatch.setattr(esapp, 'UNIT_DIR', str(units))
    fake = FakeNode(str(root))
    monkeypatch.setattr(esapp, 'sh', fake.sh)
    clock = Clock()
    monkeypatch.setattr(esapp, 'time', clock)
    fake.clock, fake.units = clock, units
    esapp.bootstrap()
    return fake


def pkg(tmp_path, version, **extra):
    d = tmp_path / ('src-' + version)
    (d / 'bin').mkdir(parents=True)
    (d / 'bin' / 'server').write_text('#!/bin/sh\nexec sleep 1000\n')
    os.chmod(d / 'bin' / 'server', 0o700)
    m = dict({'name': 'demo-sensor', 'version': version, 'run': 'bin/server --port 18080',
              'health': {'within_s': 10}, 'stable_after_s': 60, 'groups': ['dialout', 'gpio']}, **extra)
    (d / 'manifest.json').write_text(json.dumps(m))
    staged = tmp_path / 'home' / 'Downloads' / 'edgeshell'
    staged.mkdir(parents=True, exist_ok=True)
    return esapp.pack(str(d), str(staged / ('demo-sensor-%s.esapp' % version)))


def settle(node, seconds=61):
    node.clock.sleep(seconds)
    return esapp.inventory()


def test_install_builds_beside_and_switches(node, tmp_path):
    p = pkg(tmp_path, '1.0.0')
    r = esapp.install(p, esapp.read_package(p)[1])
    assert r['ok'] and r['version'] == '1.0.0' and node.current() == '1.0.0' and node.enabled
    app = os.path.join(node.root, 'apps', 'demo-sensor')
    assert not os.path.exists(p)                                               # staged copy gone
    assert os.path.isfile(os.path.join(app, 'ota', 'demo-sensor-1.0.0.esapp'))  # OTA copy kept
    vd = os.path.join(app, 'versions', '1.0.0')
    assert sorted(f for f in os.listdir(vd) if f.endswith('.sh')) == ['install.sh', 'rollback.sh', 'uninstall.sh']
    unit = (node.units / 'es-demo-sensor.service').read_text()
    assert 'ExecStart=%s/current/bin/server --port 18080' % app in unit
    assert 'es-demo-sensor' in node.users
    assert ['usermod', '-a', '-G', 'dialout', 'es-demo-sensor'] in node.calls          # only groups that exist
    with pytest.raises(esapp.Fail, match='checksum'):
        esapp.install(pkg(tmp_path / 'b', '1.1.0'), 'f' * 64)


def test_a_crashing_version_rolls_back_by_itself_and_is_never_installed_again(node, tmp_path):
    esapp.install(pkg(tmp_path, '1.0.0'))
    node.bad.add('2.0.0')
    r = esapp.install(pkg(tmp_path, '2.0.0'))
    assert not r['ok'] and r['rolled_back_to'] == '1.0.0' and node.current() == '1.0.0'
    assert 'failed to start' in r['error']
    with pytest.raises(esapp.Fail, match='failed here before'):
        esapp.install(os.path.join(node.root, 'apps', 'demo-sensor', 'ota', 'demo-sensor-2.0.0.esapp'))
    node.bad.add('9.0.0')
    a = esapp.App('demo-sensor')
    assert a.state['versions']['2.0.0']['status'] == 'failed'


def test_a_crash_with_nothing_before_stops_the_app(node, tmp_path):
    node.bad.add('1.0.0')
    r = esapp.install(pkg(tmp_path, '1.0.0'))
    assert not r['ok'] and r['rolled_back_to'] is None and node.current() is None and not node.enabled


def test_versions_become_stable_and_rollback_goes_to_the_newest_before(node, tmp_path):
    for v in ('1.0.0', '1.1.0', '1.2.0', '1.3.0'):
        assert esapp.install(pkg(tmp_path, v))['ok']
        settle(node)
    st = {v['version']: v['status'] for v in esapp.inventory()['apps'][0]['versions']}
    assert st == {'1.0.0': 'stable', '1.1.0': 'stable', '1.2.0': 'stable', '1.3.0': 'stable'}
    r = esapp.rollback('demo-sensor')
    assert r['ok'] and r['version'] == '1.2.0' and r['removed'] == '1.3.0' and node.current() == '1.2.0'
    assert not os.path.isdir(esapp.App('demo-sensor').vdir('1.3.0'))
    assert esapp.App('demo-sensor').state['versions']['1.3.0']['status'] == 'failed'
    r = esapp.activate('demo-sensor', '1.0.0')
    assert r['ok'] and node.current() == '1.0.0'
    assert esapp.App('demo-sensor').state['versions']['1.0.0']['status'] == 'stable'   # keeps what it earned
    with pytest.raises(esapp.Fail):
        esapp.activate('demo-sensor', '7.0.0')
    assert esapp.activate('demo-sensor', '1.0.0')['note'] == 'already running'


def test_a_restarting_service_does_not_become_stable(node, tmp_path):
    esapp.install(pkg(tmp_path, '1.0.0'))
    node.restarts = 2
    settle(node)
    assert esapp.App('demo-sensor').state['versions']['1.0.0']['status'] == 'active'
    with pytest.raises(esapp.Fail, match='no other stable version'):
        esapp.rollback('demo-sensor')


def test_cleanup_never_removes_the_running_or_newest_three_stable(node, tmp_path):
    for v in ('1.0.0', '1.1.0', '1.2.0', '1.3.0', '1.4.0'):
        esapp.install(pkg(tmp_path, v))
        settle(node)
    node.bad.add('1.5.0')
    esapp.install(pkg(tmp_path, '1.5.0'))
    r = esapp.prune('demo-sensor')
    assert r['removed'] == ['1.0.0', '1.1.0', '1.5.0'] and r['kept_protected'] == []
    r = esapp.prune('demo-sensor', ['1.2.0', '1.3.0', '1.4.0'])
    assert r['removed'] == [] and r['kept_protected'] == ['1.2.0', '1.3.0', '1.4.0']
    ota = os.listdir(os.path.join(node.root, 'apps', 'demo-sensor', 'ota'))
    assert sorted(ota) == ['demo-sensor-1.2.0.esapp', 'demo-sensor-1.3.0.esapp', 'demo-sensor-1.4.0.esapp']


def test_the_same_build_again_is_a_switch_and_a_changed_build_is_refused(node, tmp_path):
    p1 = pkg(tmp_path, '1.0.0')
    data = open(p1, 'rb').read()
    esapp.install(p1)
    esapp.install(pkg(tmp_path, '1.1.0'))
    again = tmp_path / 'again.esapp'
    again.write_bytes(data)
    assert esapp.install(str(again))['version'] == '1.0.0' and node.current() == '1.0.0'
    assert esapp.install(str(again))['note'] == 'already running'
    with pytest.raises(esapp.Fail, match='different contents'):
        esapp.install(pkg(tmp_path / 'other', '1.0.0', description='changed'))


def test_uninstall_removes_everything_and_can_keep_the_data(node, tmp_path):
    esapp.install(pkg(tmp_path, '1.0.0'))
    data = os.path.join(node.root, 'apps', 'demo-sensor', 'data')
    open(os.path.join(data, 'readings.db'), 'w').write('x')
    r = esapp.uninstall('demo-sensor', keep_data=True)
    assert r['removed'] and os.path.isfile(os.path.join(r['data_kept_in'], 'readings.db'))
    assert not os.path.exists(os.path.join(node.root, 'apps', 'demo-sensor'))
    assert not (node.units / 'es-demo-sensor.service').exists() and 'es-demo-sensor' not in node.users
    with pytest.raises(esapp.Fail, match='not installed'):
        esapp.uninstall('demo-sensor')


def test_a_user_that_was_there_before_is_left_alone(node, tmp_path):
    node.users.add('es-demo-sensor')
    esapp.install(pkg(tmp_path, '1.0.0'))
    esapp.uninstall('demo-sensor')
    assert 'es-demo-sensor' in node.users


def test_inventory_and_the_command_line(node, tmp_path, capsys):
    p = pkg(tmp_path, '1.0.0', ports=[18080], description='demo')
    assert esapp.main(['check', p]) == 0
    assert esapp.main(['install', p]) == 0
    assert esapp.main(['inventory']) == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    inv = json.loads(out[len('ESAPP_RESULT '):])
    a = inv['apps'][0]
    assert a['current'] == '1.0.0' and a['service'] == 'active' and a['ports'] == [18080]
    assert a['versions'][0]['protected'] and a['versions'][0]['size'] > 0
    assert esapp.main(['status', 'demo-sensor']) == 0
    assert esapp.main(['rollback', 'demo-sensor']) == 1                       # nothing to go back to: refused, not a crash
    assert 'no other stable version' in capsys.readouterr().out
    assert esapp.main(['version']) == 0 and esapp.main([]) == 2
    assert esapp.main(['prune', 'demo-sensor', '--versions', '1.0.0']) == 0
    assert esapp.main(['uninstall', 'demo-sensor']) == 0
