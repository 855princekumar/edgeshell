"""Apps on the edge (ADR 0017, docs/APPS.md).

The server side of esapp: a bucket that keeps every package uploaded (for the
audit, until an administrator removes it), rollouts that go to a few canary
nodes first and stop by themselves when nodes fail, one-click rollback,
cleanup and full removal on nodes, and each node's inventory of versions.

On the node everything is done by esapp (app/agents/esapp/esapp.py): files
arrive in ~/Downloads/edgeshell and are installed into /opt/edgeshell only.
"""
import hashlib
import importlib.util
import json
import os
import posixpath
import shlex
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import current_app

from app import db

ESAPP_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'agents', 'esapp', 'esapp.py')
NODE_ESAPP = '/opt/edgeshell/bin/esapp'
STAGE = 'Downloads/edgeshell'               # under the SSH user's home, as for Deploy Files
RESULT = 'ESAPP_RESULT '
ROLLOUT_WORKERS = 4
KINDS = ('activate', 'rollback', 'prune', 'uninstall')


def esapp():
    """The very esapp.py that goes to the nodes, used here to check packages the same way."""
    spec = importlib.util.spec_from_file_location('esapp_tool', ESAPP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ES = esapp()


class Refused(Exception):
    """A package refused, with a sentence written for people (esapp's own text, never a library message)."""

    def __init__(self, text):
        super().__init__(text)
        self.text = text


def bucket():
    path = os.path.join(current_app.config['UPLOAD_FOLDER'], 'packages')
    os.makedirs(path, exist_ok=True)
    return path


def package_path(pkg):
    return os.path.join(bucket(), pkg.name, pkg.version, pkg.filename)


# ------------------------------------------------------------------ the bucket
def store(stream, user, note=None):
    """Check and keep an uploaded package. Returns (package, created).
    Raises Refused with a reason a person can act on."""
    from app.models.apps import AppPackage
    tmp = os.path.join(bucket(), '.incoming-%s.esapp' % uuid.uuid4().hex)
    try:
        with open(tmp, 'wb') as out:
            shutil.copyfileobj(stream, out, 1 << 20)
        try:
            m, digest = ES.read_package(tmp)
        except ES.Fail as e:
            raise Refused(e.text)
        have = AppPackage.query.filter_by(name=m['name'], version=m['version']).first()
        if have and have.sha256 != digest:
            raise Refused('%s %s was uploaded before with different contents. Versions never change: '
                             'give the new build a new version.' % (m['name'], m['version']))
        fname = '%s-%s%s' % (m['name'], m['version'], ES.EXT)
        dest_dir = os.path.join(bucket(), m['name'], m['version'])
        os.makedirs(dest_dir, exist_ok=True)
        os.replace(tmp, os.path.join(dest_dir, fname))
        if have:
            if have.removed_at:                       # the same build again, after it left the bucket
                have.removed_at = have.removed_by = None
                db.session.commit()
            return have, False
        pkg = AppPackage(name=m['name'], version=m['version'], sha256=digest,
                         size=os.path.getsize(os.path.join(dest_dir, fname)),
                         manifest=json.dumps(m, sort_keys=True), filename=fname,
                         uploaded_by=user, note=(note or '')[:300] or None)
        db.session.add(pkg)
        db.session.commit()
        return pkg, True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def remove_from_bucket(pkg, user):
    """Delete the file after the audit; the record of the upload stays."""
    path = package_path(pkg)
    if os.path.exists(path):
        os.remove(path)
    for d in (os.path.dirname(path), os.path.dirname(os.path.dirname(path))):
        try:
            os.rmdir(d)
        except OSError:
            pass
    pkg.removed_at, pkg.removed_by = datetime.utcnow(), user
    db.session.commit()


def verify(pkg):
    """The file in the bucket is the one uploaded (checksum)."""
    path = package_path(pkg)
    if not os.path.isfile(path):
        return False
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest() == pkg.sha256


# ------------------------------------------------------------------ one node
def _target(node):
    from app.services import collector
    return collector.target_for(node)


def _run(node, cmd, timeout=900):
    """(result dict from esapp, full output)."""
    from app.services import collector
    t = _target(node)
    r = collector.run_command(t, cmd, timeout=timeout, sudo_password=t['password'], get_pty=False)
    out = (r.get('stdout') or '').replace('\r', '')
    lines = [line for line in out.splitlines() if line.startswith(RESULT)]
    if lines:
        try:
            return json.loads(lines[-1][len(RESULT):]), out
        except ValueError:
            pass
    return {'ok': False, 'error': (r.get('error') or out.strip()[-300:] or 'no answer from esapp')}, out


def _put(node, local_path, name):
    """SFTP a file into ~/Downloads/edgeshell on the node (as the SSH user); returns its path."""
    from app.services.collector import POOL
    t = _target(node)
    with POOL.lock(t['id']):
        client, _ = POOL.get(t)
    sftp = client.open_sftp()
    try:
        home = sftp.normalize('.')
        path = home
        for part in STAGE.split('/'):
            path = posixpath.join(path, part)
            try:
                sftp.stat(path)
            except IOError:
                sftp.mkdir(path, 0o750)
        final = posixpath.join(path, name)
        part = posixpath.join(path, '.' + name + '.part')
        sftp.put(local_path, part)
        sftp.chmod(part, 0o600)
        sftp.posix_rename(part, final)
        return final
    finally:
        sftp.close()


def _version_tuple(v):
    try:
        return tuple(int(x) for x in (v or '').split('.'))
    except ValueError:
        return ()


def ensure_esapp(node):
    """esapp in /opt/edgeshell/bin at this EdgeShell's version (installs or updates it)."""
    res, _ = _run(node, 'sudo python3 %s version 2>/dev/null || true' % NODE_ESAPP, timeout=30)
    have = res.get('esapp') if res.get('ok') else None
    if have and _version_tuple(have) >= _version_tuple(ES.ESAPP_VERSION):
        return have
    path = _put(node, ESAPP_PATH, '.esapp-%s.py' % ES.ESAPP_VERSION)
    res, out = _run(node, 'sudo python3 %s bootstrap; rc=$?; rm -f %s; exit $rc' % (shlex.quote(path), shlex.quote(path)),
                    timeout=60)
    if not res.get('ok'):
        raise RuntimeError('could not set up esapp on the node: %s' % (res.get('error') or out[-200:]))
    node.esapp_version = res.get('esapp')
    db.session.commit()
    return node.esapp_version


def refresh_inventory(node):
    """Read what the node has (versions, states, service) into node.apps_inventory."""
    res, _ = _run(node, 'test -x %s || { echo "%s{\\"ok\\": true, \\"apps\\": [], \\"esapp\\": null}"; exit 0; }; '
                        'sudo python3 %s inventory' % (NODE_ESAPP, RESULT, NODE_ESAPP), timeout=120)
    if res.get('ok'):
        node.apps_inventory = json.dumps({'apps': res.get('apps') or [], 'system': res.get('system') or {}})
        node.esapp_version = res.get('esapp')
        node.apps_checked_at = datetime.utcnow()
        db.session.commit()
    return res


def install_on(node, pkg):
    """Put the package on the node and install it there (auto-revert on a bad version).
    Returns esapp's result."""
    ensure_esapp(node)
    remote = _put(node, package_path(pkg), pkg.filename)
    res, out = _run(node, 'sudo python3 %s install %s --sha256 %s' % (NODE_ESAPP, shlex.quote(remote), pkg.sha256))
    res.setdefault('output', out[-1500:])
    try:
        refresh_inventory(node)
    except Exception:                                  # noqa: BLE001 - the install result matters more
        current_app.logger.warning('apps: inventory after install on %s failed', node.name, exc_info=True)
    return res


def act_on(node, kind, name, version=None, versions=None, keep_data=False):
    """activate | rollback | prune | uninstall one app on one node."""
    if kind not in KINDS or not ES.NAME_RE.match(name or ''):
        raise ValueError('not a valid action or app name')
    for v in ([version] if version else []) + list(versions or []):
        if not ES.VERSION_RE.match(v):
            raise ValueError('not a version: %r' % v)
    args = [kind, name]
    if kind == 'activate':
        if not version:
            raise ValueError('which version?')
        args.append(version)
    elif kind == 'rollback' and version:
        args += ['--to', version]
    elif kind == 'prune' and versions:
        args += ['--versions', ','.join(versions)]
    elif kind == 'uninstall' and keep_data:
        args.append('--keep-data')
    res, out = _run(node, 'sudo python3 %s %s' % (NODE_ESAPP, ' '.join(shlex.quote(a) for a in args)))
    res.setdefault('output', out[-1500:])
    try:
        refresh_inventory(node)
    except Exception:                                  # noqa: BLE001
        current_app.logger.warning('apps: inventory after %s on %s failed', kind, node.name, exc_info=True)
    return res


def inventory_of(node):
    try:
        return json.loads(node.apps_inventory or '{}').get('apps') or []
    except ValueError:
        return []


# ------------------------------------------------------------------ fleet jobs
def _summary(res):
    if res.get('ok'):
        bits = [res.get('version') or '', res.get('health') or res.get('note') or '']
        if res.get('removed'):
            bits.append('removed %s' % (', '.join(res['removed']) if isinstance(res['removed'], list) else res['removed']))
        return ('done ' + ' '.join(b for b in bits if b)).strip()[:300]
    if res.get('rolled_back_to'):
        return ('not healthy (%s): rolled back to %s' % (res.get('error'), res['rolled_back_to']))[:300]
    return (res.get('error') or 'failed')[:300]


def start_rollout(pkg, nodes, user_id, username, canary=1, stop_pct=20):
    """Canary nodes first, one at a time; the rest only if every canary is healthy,
    a few at a time, stopping when more than stop_pct % of finished nodes failed."""
    from app.models.apps import AppAction
    from app.services import jobs
    from app.services.activity_service import log_activity
    nodes = sorted(nodes, key=lambda n: n.name)
    canary = max(1, min(int(canary), len(nodes)))
    stop_pct = max(0, min(int(stop_pct), 100))
    action = AppAction(kind='rollout', app=pkg.name, version=pkg.version, package_id=pkg.id, by=username,
                       nodes=json.dumps([n.name for n in nodes]),
                       options=json.dumps({'canary': canary, 'stop_after_pct': stop_pct}))
    db.session.add(action)
    db.session.commit()
    aid, pid, ids = action.id, pkg.id, [n.id for n in nodes]
    app = current_app._get_current_object()

    def fn(task):
        from app.models.apps import AppAction, AppPackage
        from app.models.node import Node
        p = db.session.get(AppPackage, pid)
        results, lock, stop = {}, threading.Lock(), threading.Event()
        if not verify(p):
            task.log('the package in the bucket does not match its checksum: nothing was sent')
            results['*'] = {'ok': False, 'message': 'package checksum mismatch'}
            _finish(aid, results, 'failed', 'stopped before any node: the package file changed')
            return
        for nid in ids:
            n = db.session.get(Node, nid)
            task.item(nid, name=n.name, state='waiting', step='canary' if ids.index(nid) < canary else 'fleet')

        def one(nid):
            with app.app_context():
                try:
                    n = db.session.get(Node, nid)
                    if stop.is_set():
                        r = {'ok': False, 'skipped': True, 'message': 'not started: the rollout stopped'}
                        task.item(nid, state='skipped', message=r['message'])
                    elif n is None or n.is_deleted or n.status != 'online' or n.login_paused_at:
                        r = {'ok': False, 'skipped': True, 'message': 'skipped: the node is not reachable now'}
                        task.item(nid, state='skipped', message=r['message'])
                    else:
                        task.item(nid, state='running', message='installing %s %s' % (p.name, p.version))
                        try:
                            res = install_on(n, p)
                        except Exception as e:             # noqa: BLE001 - one node never stops the job by itself
                            from app.services.errors import describe
                            res = {'ok': False, 'error': describe(e, 'install on %s' % n.name)}
                        r = {'ok': bool(res.get('ok')), 'message': _summary(res),
                             'rolled_back_to': res.get('rolled_back_to')}
                        task.item(nid, state='ok' if r['ok'] else 'failed', message=r['message'])
                        task.log('%s: %s' % (n.name, r['message']))
                        log_activity('App rollout', node=n, command='%s %s' % (p.name, p.version),
                                     response=r['message'], status='success' if r['ok'] else 'failure', user=username)
                    with lock:
                        results[n.name if n else str(nid)] = r
                        tried = [x for x in results.values() if not x.get('skipped')]
                        failed = [x for x in tried if not x['ok']]
                        if tried and len(tried) >= 3 and 100.0 * len(failed) / len(tried) > stop_pct and not stop.is_set():
                            stop.set()
                            task.log('stopping: %d of %d nodes failed (more than %d%%)' % (len(failed), len(tried), stop_pct))
                    task.finish_item(r['ok'])
                finally:
                    db.session.remove()

        task.log('canary: %d node(s) first' % canary)
        for nid in ids[:canary]:
            one(nid)
        tried = [x for x in results.values() if not x.get('skipped')]
        if not tried or not all(x['ok'] for x in tried):
            stop.set()
            task.log('stopping: the canary did not pass, the rest of the fleet is untouched')
        rest = ids[canary:]
        if rest:
            with ThreadPoolExecutor(max_workers=ROLLOUT_WORKERS) as pool:
                list(pool.map(one, rest))
        ok = sum(1 for x in results.values() if x['ok'])
        skipped = sum(1 for x in results.values() if x.get('skipped'))
        status = 'stopped' if stop.is_set() else 'done'
        _finish(aid, results, status, '%d of %d nodes on %s %s%s%s' % (
            ok, len(ids), p.name, p.version, ', %d skipped' % skipped if skipped else '',
            ', stopped' if stop.is_set() else ''))

    return jobs.run_task('app-rollout', 'Roll out %s %s' % (pkg.name, pkg.version), user_id, len(ids), fn,
                         summary=lambda job: '%d ok, %d failed' % (job['ok'], job['failed'])), aid


def start_action(kind, name, nodes, user_id, username, version=None, versions=None, keep_data=False):
    """rollback / activate / prune / uninstall on one or many nodes, a few at a time."""
    from app.models.apps import AppAction
    from app.services import jobs
    from app.services.activity_service import log_activity
    act_on_check = ES.NAME_RE.match(name or '') and kind in KINDS
    if not act_on_check:
        raise ValueError('not a valid action or app name')
    action = AppAction(kind=kind, app=name, version=version, by=username,
                       nodes=json.dumps([n.name for n in nodes]),
                       options=json.dumps({'versions': versions or [], 'keep_data': bool(keep_data)}))
    db.session.add(action)
    db.session.commit()
    aid, ids = action.id, [n.id for n in nodes]
    app = current_app._get_current_object()

    def fn(task):
        from app.models.node import Node
        results, lock = {}, threading.Lock()

        def one(nid):
            with app.app_context():
                try:
                    n = db.session.get(Node, nid)
                    task.item(nid, name=n.name, state='running', message=kind)
                    if n.status != 'online' or n.login_paused_at:
                        r = {'ok': False, 'skipped': True, 'message': 'skipped: the node is not reachable now'}
                    else:
                        try:
                            res = act_on(n, kind, name, version, versions, keep_data)
                        except Exception as e:             # noqa: BLE001
                            from app.services.errors import describe
                            res = {'ok': False, 'error': describe(e, '%s on %s' % (kind, n.name))}
                        r = {'ok': bool(res.get('ok')), 'message': _summary(res)}
                        log_activity('App %s' % kind, node=n, command='%s %s' % (name, version or ','.join(versions or [])),
                                     response=r['message'], status='success' if r['ok'] else 'failure', user=username)
                    task.item(nid, state='ok' if r['ok'] else ('skipped' if r.get('skipped') else 'failed'), message=r['message'])
                    task.log('%s: %s' % (n.name, r['message']))
                    with lock:
                        results[n.name] = r
                    task.finish_item(r['ok'])
                finally:
                    db.session.remove()

        with ThreadPoolExecutor(max_workers=ROLLOUT_WORKERS) as pool:
            list(pool.map(one, ids))
        ok = sum(1 for x in results.values() if x['ok'])
        _finish(aid, results, 'done', '%s %s: %d of %d nodes' % (kind, name, ok, len(ids)))

    title = {'rollback': 'Roll back', 'activate': 'Switch', 'prune': 'Clean up', 'uninstall': 'Remove'}[kind]
    return jobs.run_task('app-' + kind, '%s %s' % (title, name), user_id, len(ids), fn,
                         summary=lambda job: '%d ok, %d failed' % (job['ok'], job['failed'])), aid


def start_scan(nodes, user_id):
    """Read every node's apps and versions (read-only)."""
    from app.services import jobs

    def work(nid):
        from app.models.node import Node
        n = db.session.get(Node, nid)
        if n is None or n.status != 'online' or n.login_paused_at:
            return False
        return bool(refresh_inventory(n).get('ok'))
    return jobs.run_each('app-scan', 'Read apps on %d node(s)' % len(nodes), user_id, [n.id for n in nodes], work,
                         workers=8, summary=lambda job: '%d read, %d not reachable' % (job['ok'], job['failed']))


def _finish(aid, results, status, summary):
    from app.models.apps import AppAction
    a = db.session.get(AppAction, aid)
    a.results = json.dumps(results, sort_keys=True)
    a.status, a.summary, a.finished_at = status, summary[:300], datetime.utcnow()
    db.session.commit()
