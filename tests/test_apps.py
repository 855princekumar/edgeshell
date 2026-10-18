"""Apps on the edge (ADR 0017): packages are checked like a node checks them
and kept in the bucket for the audit, rollouts go to canary nodes first and
stop by themselves, node actions build safe commands, only administrators
change anything, and Deploy Files writes only to ~/Downloads/edgeshell."""
import io
import json
import os
import tarfile

import pytest

from app import create_app, db, limiter
from app.models.node import Node
from app.models.user import User


@pytest.fixture
def app(tmp_path):
    app = create_app('testing')
    app.config.update(UPLOAD_FOLDER=str(tmp_path / 'uploads'))
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
        for i in range(6):
            db.session.add(Node(name=f'pi-0{i}', ip_address=f'10.0.0.{i + 1}', username='pi',
                                status='offline' if i == 5 else 'online'))
        db.session.commit()
    yield app


def login(app, user='admin', pw='admin123'):
    c = app.test_client()
    c.post('/login', data={'username': user, 'password': pw})
    return c


def make_pkg(tmp_path, version='1.0.0', name='demo-sensor', extra=None, body=b'print(1)\n'):
    from app.services import app_service as svc
    d = tmp_path / f'src-{name}-{version}'
    (d / 'app').mkdir(parents=True)
    (d / 'app' / 'main.py').write_bytes(body)
    m = {'name': name, 'version': version, 'run': 'python app/main.py', 'description': 'test app'}
    m.update(extra or {})
    (d / 'manifest.json').write_text(json.dumps(m))
    return svc.ES.pack(str(d), str(tmp_path / f'{name}-{version}.esapp'))


def upload(c, path, note=''):
    with open(path, 'rb') as fh:
        return c.post('/apps/api/packages', data={'package': (io.BytesIO(fh.read()), os.path.basename(path)), 'note': note},
                      content_type='multipart/form-data')


@pytest.fixture
def sync_jobs(monkeypatch):
    """Run background jobs inline, one node at a time, so the order is known."""
    from app.services import jobs, app_service as svc
    monkeypatch.setattr(svc, 'ROLLOUT_WORKERS', 1)
    done = {}

    def run_task(kind, title, owner_id, total, fn, summary=None):
        job = {'id': 'j1', 'kind': kind, 'title': title, 'owner': owner_id, 'total': total, 'done': 0, 'ok': 0,
               'failed': 0, 'state': 'running', 'message': '', 'started': 0, 'finished': None, 'items': [], 'log': []}
        fn(jobs.Task(job))
        job['state'] = 'done'
        done['job'] = job
        return 'j1'
    monkeypatch.setattr(jobs, 'run_task', run_task)
    return done


# ------------------------------------------------------------------ the bucket
def test_a_package_is_checked_kept_in_the_bucket_and_never_changes(app, tmp_path):
    c = login(app)
    p = make_pkg(tmp_path)
    r = upload(c, p, 'first build').get_json()
    assert r['success'] and r['created'] and r['package']['name'] == 'demo-sensor' and r['package']['in_bucket']
    stored = os.path.join(app.config['UPLOAD_FOLDER'], 'packages', 'demo-sensor', '1.0.0', 'demo-sensor-1.0.0.esapp')
    assert os.path.isfile(stored)
    assert upload(c, p).get_json()['created'] is False                            # the same build again: kept once
    other = make_pkg(tmp_path / 'x', body=b'print(2)\n')
    r = upload(c, other)
    assert r.status_code == 400 and 'new version' in r.get_json()['message']      # a version never changes
    assert not [f for f in os.listdir(os.path.join(app.config['UPLOAD_FOLDER'], 'packages')) if f.startswith('.incoming')]


def test_unsafe_or_invalid_packages_are_refused(app, tmp_path):
    c = login(app)
    bad = tmp_path / 'evil.esapp'
    with tarfile.open(bad, 'w:gz') as tf:
        data = json.dumps({'name': 'evil-app', 'version': '1.0.0', 'run': 'python x.py'}).encode()
        ti = tarfile.TarInfo('manifest.json'); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
        ti = tarfile.TarInfo('../../etc/cron.d/x'); ti.size = 1; tf.addfile(ti, io.BytesIO(b'x'))
    r = upload(c, bad)
    assert r.status_code == 400 and 'unsafe path' in r.get_json()['message']
    nope = tmp_path / 'nope.esapp'
    nope.write_bytes(b'not a package')
    assert upload(c, nope).status_code == 400
    from app.models.apps import AppPackage
    with app.app_context():
        assert AppPackage.query.count() == 0


def test_only_administrators_change_anything(app, tmp_path):
    p = make_pkg(tmp_path)
    op = login(app, 'op', 'Operator-pass-1')
    assert op.get('/apps/').status_code == 200 and op.get('/apps/api/packages').status_code == 200
    assert upload(op, p).status_code in (302, 403)
    for url, body in (('/apps/api/rollout', {'package_id': 1, 'node_ids': [1]}),
                      ('/apps/api/action', {'kind': 'rollback', 'app': 'demo-sensor', 'node_ids': [1]}),
                      ('/apps/api/packages/1/remove', {'confirm': 'demo-sensor 1.0.0'})):
        assert op.post(url, json=body).status_code == 403
    mon = login(app, 'mon', 'Operator-pass-1')
    assert mon.get('/apps/api/packages').status_code in (302, 403)


def test_removing_a_package_after_the_audit_keeps_its_record(app, tmp_path):
    c = login(app)
    pkg = upload(c, make_pkg(tmp_path)).get_json()['package']
    assert c.post(f'/apps/api/packages/{pkg["id"]}/remove', json={'confirm': 'wrong'}).status_code == 400
    assert c.post(f'/apps/api/packages/{pkg["id"]}/remove', json={'confirm': 'demo-sensor 1.0.0'}).get_json()['success']
    p = c.get('/apps/api/packages').get_json()['packages'][0]
    assert p['in_bucket'] is False and p['removed_by'] == 'admin'
    assert c.get(f'/apps/api/packages/{pkg["id"]}/download').status_code == 404
    assert c.post('/apps/api/rollout', json={'package_id': pkg['id'], 'node_ids': [1]}).status_code == 400


# ------------------------------------------------------------------ rollouts
def _rollout(app, c, tmp_path, outcomes, canary=1, stop=20, ids=None):
    from app.services import app_service as svc
    pkg = upload(c, make_pkg(tmp_path)).get_json()['package']
    seen = []

    def install_on(node, p):
        seen.append(node.name)
        return outcomes.get(node.name, {'ok': True, 'version': p.version, 'health': 'up for 20s'})
    svc.install_on = install_on
    r = c.post('/apps/api/rollout', json={'package_id': pkg['id'], 'node_ids': ids or [1, 2, 3, 4, 5, 6],
                                          'canary': canary, 'stop_after_pct': stop}).get_json()
    assert r['success']
    action = c.get('/apps/api/actions').get_json()['actions'][0]
    return seen, action


def test_a_rollout_goes_to_the_canary_first_and_skips_offline_nodes(app, tmp_path, sync_jobs, monkeypatch):
    from app.services import app_service as svc
    monkeypatch.setattr(svc, 'install_on', svc.install_on)
    c = login(app)
    seen, a = _rollout(app, c, tmp_path, {})
    assert seen == ['pi-00', 'pi-01', 'pi-02', 'pi-03', 'pi-04']
    assert a['status'] == 'done' and a['results']['pi-05']['skipped'] and a['summary'].startswith('5 of 6 nodes')
    assert a['options'] == {'canary': 1, 'stop_after_pct': 20}


def test_a_failed_canary_leaves_the_rest_of_the_fleet_untouched(app, tmp_path, sync_jobs, monkeypatch):
    from app.services import app_service as svc
    monkeypatch.setattr(svc, 'install_on', svc.install_on)
    c = login(app)
    seen, a = _rollout(app, c, tmp_path, {'pi-01': {'ok': False, 'error': 'the service crashed and restarted',
                                                   'rolled_back_to': '0.9.0'}}, canary=2)
    assert seen == ['pi-00', 'pi-01'] and a['status'] == 'stopped'
    assert 'rolled back to 0.9.0' in a['results']['pi-01']['message']
    assert all(a['results'][n]['skipped'] for n in ('pi-02', 'pi-03', 'pi-04'))


def test_a_rollout_stops_by_itself_when_too_many_nodes_fail(app, tmp_path, sync_jobs, monkeypatch):
    from app.services import app_service as svc
    monkeypatch.setattr(svc, 'install_on', svc.install_on)
    c = login(app)
    bad = {'ok': False, 'error': 'pip could not install requirements.txt'}
    seen, a = _rollout(app, c, tmp_path, {'pi-01': bad, 'pi-02': bad}, canary=1, stop=20)
    assert seen == ['pi-00', 'pi-01', 'pi-02'] and a['status'] == 'stopped'      # 2 of 3 failed: stop
    assert a['results']['pi-03']['skipped'] and a['results']['pi-04']['skipped']


def test_a_rollout_refuses_a_package_whose_file_changed(app, tmp_path, sync_jobs, monkeypatch):
    from app.services import app_service as svc
    monkeypatch.setattr(svc, 'install_on', lambda node, p: pytest.fail('nothing may be sent'))
    c = login(app)
    pkg = upload(c, make_pkg(tmp_path)).get_json()['package']
    path = os.path.join(app.config['UPLOAD_FOLDER'], 'packages', 'demo-sensor', '1.0.0', 'demo-sensor-1.0.0.esapp')
    with open(path, 'ab') as fh:
        fh.write(b'tampered')
    c.post('/apps/api/rollout', json={'package_id': pkg['id'], 'node_ids': [1]})
    a = c.get('/apps/api/actions').get_json()['actions'][0]
    assert a['status'] == 'failed' and 'package file changed' in a['summary']


# ------------------------------------------------------------------ node actions
def test_node_actions_build_safe_commands(app, tmp_path, sync_jobs, monkeypatch):
    from app.services import app_service as svc
    cmds = []
    monkeypatch.setattr(svc, '_run', lambda node, cmd, timeout=900: (cmds.append(cmd) or ({'ok': True, 'version': '1.0.0', 'apps': []}, '')))
    c = login(app)
    assert c.post('/apps/api/action', json={'kind': 'rollback', 'app': 'demo-sensor', 'node_ids': [1]}).get_json()['success']
    assert c.post('/apps/api/action', json={'kind': 'prune', 'app': 'demo-sensor', 'node_ids': [1],
                                            'versions': ['1.0.0', '2.0.1']}).get_json()['success']
    assert c.post('/apps/api/action', json={'kind': 'uninstall', 'app': 'demo-sensor', 'node_ids': [1]}).status_code == 400
    assert c.post('/apps/api/action', json={'kind': 'uninstall', 'app': 'demo-sensor', 'node_ids': [1],
                                            'confirm': 'demo-sensor', 'keep_data': True}).get_json()['success']
    assert c.post('/apps/api/action', json={'kind': 'activate', 'app': 'demo-sensor', 'node_ids': [1],
                                            'version': '1.0.0; rm -rf /'}).status_code == 400
    assert c.post('/apps/api/action', json={'kind': 'prune', 'app': 'x; reboot', 'node_ids': [1]}).status_code == 400
    acts = [x for x in cmds if 'inventory' not in x]
    assert acts == ['sudo python3 /opt/edgeshell/bin/esapp rollback demo-sensor',
                    'sudo python3 /opt/edgeshell/bin/esapp prune demo-sensor --versions 1.0.0,2.0.1',
                    'sudo python3 /opt/edgeshell/bin/esapp uninstall demo-sensor --keep-data']


def test_the_inventory_is_read_from_each_node(app, monkeypatch):
    from app.services import app_service as svc
    inv = {'ok': True, 'esapp': '1.0.0', 'apps': [{'name': 'demo-sensor', 'current': '2.0.1', 'service': 'active',
                                                   'versions': [{'version': '2.0.1', 'status': 'stable', 'protected': True}]}]}
    monkeypatch.setattr(svc, '_run', lambda node, cmd, timeout=900: (inv, ''))
    with app.app_context():
        n = Node.query.filter_by(name='pi-00').one()
        assert svc.refresh_inventory(n)['ok'] and n.esapp_version == '1.0.0'
    d = login(app).get('/apps/api/nodes').get_json()['nodes']
    row = [x for x in d if x['name'] == 'pi-00'][0]
    assert row['apps'][0]['current'] == '2.0.1' and row['checked_at']


def test_esapp_is_set_up_on_a_node_only_when_missing_or_older(app, monkeypatch):
    from app.services import app_service as svc
    calls = []
    answers = iter([({'ok': True, 'esapp': '0.9.0'}, ''), ({'ok': True, 'esapp': svc.ES.ESAPP_VERSION}, ''),
                    ({'ok': True, 'esapp': svc.ES.ESAPP_VERSION}, '')])
    monkeypatch.setattr(svc, '_run', lambda node, cmd, timeout=900: (calls.append(cmd) or next(answers)))
    monkeypatch.setattr(svc, '_put', lambda node, path, name: calls.append('put ' + name) or '/home/pi/Downloads/edgeshell/' + name)
    with app.app_context():
        n = Node.query.first()
        svc.ensure_esapp(n)                                                      # older: updated
        assert calls[1].startswith('put .esapp-') and 'bootstrap' in calls[2] and 'rm -f' in calls[2]
        calls.clear()
        svc.ensure_esapp(n)                                                      # current: nothing sent
        assert len(calls) == 1 and 'version' in calls[0]


# ------------------------------------------------------------------ Deploy Files: only ~/Downloads/edgeshell
def test_files_go_only_to_downloads_edgeshell(app):
    from app.services.ssh_service import clean_subfolder
    assert clean_subfolder('') == '' and clean_subfolder('configs/site-a') == 'configs/site-a'
    assert clean_subfolder('~/Downloads/edgeshell/configs') == 'configs'
    for bad in ('/etc', '/opt/app', '../x', 'a/../../b', 'a b', '~/.ssh', 'a;rm'):
        with pytest.raises(ValueError):
            clean_subfolder(bad)
    c = login(app)
    r = c.post('/files/upload', data={'node_ids': '1', 'target_path': '/etc/',
                                      'files': (io.BytesIO(b'x'), 'x.conf')}, content_type='multipart/form-data')
    assert r.status_code == 400 and 'Downloads/edgeshell' in r.get_json()['error']
