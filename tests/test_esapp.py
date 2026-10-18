"""esapp's own rules, on any OS: manifests, packages, the unit it writes,
and which versions it always keeps. The real node behaviour (systemd, venv,
auto-revert, uninstall) is tests/esapp_e2e.py on a Debian systemd node."""
import io
import json
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app', 'agents', 'esapp'))
import esapp  # noqa: E402

GOOD = {'name': 'demo-sensor', 'version': '1.2.0', 'run': 'python app/main.py'}


def test_a_minimal_manifest_gets_safe_defaults():
    m = esapp.check_manifest(dict(GOOD))
    assert m['run'] == ['python', 'app/main.py'] and m['health'] == {'within_s': 20}
    assert m['stable_after_s'] == 600 and m['groups'] == [] and m['env'] == {} and m['restart_sec'] == 5


@pytest.mark.parametrize('change, words', [
    ({'name': 'Demo'}, 'name:'), ({'name': 'ab'}, 'name:'), ({'version': 'latest'}, 'version:'),
    ({'run': 'python app/main.py; rm -rf /'}, 'run:'), ({'run': '../../bin/sh'}, 'outside'),
    ({'run': 'sh -c $(curl x)'}, 'run:'), ({'requirements': '/etc/passwd'}, 'requirements:'),
    ({'env': {'lower': 'x'}}, 'env:'), ({'env': {'A': 'x"y'}}, 'env:'), ({'env': {'A': 'x\ny'}}, 'env:'),
    ({'health': {'http': 'http://example.com/'}}, 'health.http'), ({'health': {'within_s': 1}}, 'within_s'),
    ({'groups': ['sudo']}, 'groups:'), ({'groups': ['root']}, 'groups:'), ({'memory_max': 'lots'}, 'memory_max'),
    ({'ports': [70000]}, 'ports:'), ({'runn': 'x'}, 'unknown key'),
])
def test_a_manifest_that_could_escape_or_is_wrong_is_refused(change, words):
    m = dict(GOOD)
    m.update(change)
    with pytest.raises(esapp.Fail) as e:
        esapp.check_manifest(m)
    assert words in str(e.value)


def _tar(path, members):
    with tarfile.open(path, 'w:gz') as tf:
        for name, data, kind in members:
            ti = tarfile.TarInfo(name)
            if kind == 'sym':
                ti.type, ti.linkname = tarfile.SYMTYPE, '/etc/shadow'
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))


@pytest.mark.parametrize('member', [('../x', b'x', 'f'), ('/etc/x', b'x', 'f'), ('app/link', b'', 'sym'),
                                    ('a/../../x', b'x', 'f')])
def test_a_package_with_links_or_paths_outside_is_refused(tmp_path, member):
    p = tmp_path / 'p.esapp'
    _tar(p, [('manifest.json', json.dumps(GOOD).encode(), 'f'), member])
    with pytest.raises(esapp.Fail):
        esapp.read_package(str(p))


def test_pack_builds_what_a_node_accepts_and_leaves_out_build_junk(tmp_path):
    d = tmp_path / 'src'
    (d / 'app' / '__pycache__').mkdir(parents=True)
    (d / 'venv' / 'bin').mkdir(parents=True)
    (d / 'app' / 'main.py').write_text('print(1)\n')
    (d / 'app' / '__pycache__' / 'main.cpython-312.pyc').write_bytes(b'x')
    (d / 'venv' / 'bin' / 'python').write_text('x')
    (d / 'manifest.json').write_text(json.dumps(GOOD))
    out = esapp.pack(str(d), str(tmp_path / 'demo.esapp'))
    m, digest = esapp.read_package(out)
    assert m['name'] == 'demo-sensor' and len(digest) == 64
    with tarfile.open(out) as tf:
        assert sorted(tf.getnames()) == ['app/main.py', 'manifest.json']
    ex = tmp_path / 'ex'
    esapp.extract(out, str(ex))
    assert (ex / 'app' / 'main.py').read_text() == 'print(1)\n'


@pytest.mark.skipif(os.name == 'nt', reason='node paths are POSIX paths')
def test_the_unit_is_hardened_and_runs_from_current(tmp_path, monkeypatch):
    monkeypatch.setattr(esapp, 'ROOT', '/opt/edgeshell')
    m = esapp.check_manifest(dict(GOOD, env={'GREETING': 'hello 100%'}, memory_max='128M', groups=['dialout']))
    m['_venv'] = True
    text = esapp.unit_text(esapp.App('demo-sensor'), m)
    for line in ('User=es-demo-sensor', 'WorkingDirectory=/opt/edgeshell/apps/demo-sensor/current',
                 'ExecStart=/opt/edgeshell/apps/demo-sensor/current/venv/bin/python app/main.py',
                 'ProtectSystem=strict', 'NoNewPrivileges=yes', 'ProtectHome=yes',
                 'ReadWritePaths=/opt/edgeshell/apps/demo-sensor/data', 'Restart=on-failure',
                 'Environment="GREETING=hello 100%%"', 'MemoryMax=128M', 'WantedBy=multi-user.target'):
        assert line in text.splitlines(), line


def test_the_running_and_newest_three_stable_versions_are_always_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(esapp, 'ROOT', str(tmp_path))
    a = esapp.App('demo-sensor')
    for i, (v, st) in enumerate([('1.0.0', 'stable'), ('1.1.0', 'stable'), ('1.2.0', 'failed'), ('2.0.0', 'stable'),
                                 ('2.1.0', 'stable'), ('3.0.0', 'active')]):
        os.makedirs(a.vdir(v))
        a.state['versions'][v] = {'status': st, 'stable_at': i if st == 'stable' else None}
    monkeypatch.setattr(a, 'current', lambda: '3.0.0')
    assert esapp.protected(a) == {'3.0.0', '2.1.0', '2.0.0', '1.1.0'}
    assert a.stable() == ['2.1.0', '2.0.0', '1.1.0', '1.0.0']
