#!/usr/bin/env python3
"""esapp: EdgeShell's app manager on a node. Python 3.7+, standard library only.

Every app EdgeShell installs lives in one folder and runs as one systemd
service, so nothing else on the node is touched:

  /opt/edgeshell/bin/esapp                    this tool
  /opt/edgeshell/apps/NAME/
      versions/VERSION/                       one folder per version, read-only
          app files, venv/ (its own Python packages), manifest.json,
          es-NAME.service (its unit), install.sh, rollback.sh, uninstall.sh
      current -> versions/VERSION             switched atomically
      data/                                   the app's own files, kept across versions
      ota/NAME-VERSION.esapp                  every package received, as received
      state.json, esapp.log                   what is installed, what happened
  /etc/systemd/system/es-NAME.service         the only file outside /opt/edgeshell
  user es-NAME                                the app runs as this system user

Packages arrive in ~/Downloads/edgeshell (EdgeShell's only upload folder) and
are moved into ota/. A new version is built next to the running one, switched
in, and watched; if it does not stay up (or its health check fails) the
previous version comes back by itself. The newest three stable versions are
always kept for a one-click rollback.

  esapp pack DIR [-o OUT]            build NAME-VERSION.esapp from a folder (any OS)
  esapp check PACKAGE                validate a package
  esapp bootstrap                    install this tool in /opt/edgeshell/bin
  esapp install PACKAGE [--sha256 X] [--no-activate]
  esapp activate NAME VERSION        switch to an installed version
  esapp rollback NAME [--to VERSION] [--keep-failed]
  esapp prune NAME [--versions A,B]  remove old versions (never the newest 3 stable)
  esapp uninstall NAME [--keep-data] remove the app completely
  esapp inventory | status NAME | logs NAME [-n N] | version

The last line of every command is `ESAPP_RESULT {json}` for EdgeShell.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ESAPP_VERSION = '1.0.0'
ROOT = os.environ.get('ESAPP_ROOT', '/opt/edgeshell')          # tests point this elsewhere
UNIT_DIR = os.environ.get('ESAPP_UNIT_DIR', '/etc/systemd/system')
MIN_STABLE = 3                     # stable versions always kept on the node
NAME_RE = re.compile(r'^[a-z][a-z0-9-]{1,30}[a-z0-9]$')
VERSION_RE = re.compile(r'^[0-9]+(\.[0-9]+){0,3}([.+-][A-Za-z0-9.]{1,20})?$')
ENV_KEY_RE = re.compile(r'^[A-Z_][A-Z0-9_]{0,63}$')
ARG_RE = re.compile(r'^[A-Za-z0-9_./:=,@+%-]{1,200}$')       # nothing systemd would read as quoting or a variable
SIZE_RE = re.compile(r'^[0-9]{1,6}[KMG]$')
PCT_RE = re.compile(r'^[0-9]{1,3}%$')
GROUPS = ('dialout', 'gpio', 'i2c', 'spi', 'video', 'audio', 'plugdev', 'input', 'bluetooth', 'netdev')
KEYS = {'name', 'version', 'description', 'run', 'requirements', 'env', 'health', 'stable_after_s',
        'groups', 'ports', 'memory_max', 'cpu_quota', 'restart_sec'}
MAX_MEMBERS, MAX_BYTES = 20000, 1024 * 1024 * 1024
SKIP_DIRS = {'.git', '__pycache__', 'venv', '.venv', 'node_modules', '.mypy_cache', '.pytest_cache'}
EXT = '.esapp'


class Fail(Exception):
    """A refusal with a plain reason; nothing on the node was left half done.
    .text is esapp's own sentence, never a library message or a traceback."""

    def __init__(self, text):
        super().__init__(text)
        self.text = text


# ------------------------------------------------------------------ manifest and package
def check_manifest(m):
    """The manifest, normalized; raises Fail with every problem found."""
    if not isinstance(m, dict):
        raise Fail('manifest.json must be a JSON object')
    bad = []
    for k in sorted(set(m) - KEYS):
        bad.append('unknown key %r' % k)
    name, version = m.get('name'), m.get('version')
    if not isinstance(name, str) or not NAME_RE.match(name):
        bad.append('name: 3 to 32 of a-z, 0-9 and -, starting with a letter')
    if not isinstance(version, str) or not VERSION_RE.match(version):
        bad.append('version: like 1.2.0 (up to four numbers, an optional -suffix)')
    run = m.get('run')
    if isinstance(run, str):
        run = run.split()
    if not isinstance(run, list) or not run or not all(isinstance(a, str) and ARG_RE.match(a) for a in run):
        bad.append('run: the command, for example "python app/main.py" (letters, digits and ./:=,@+%-_ only)')
    elif '..' in run[0].split('/'):
        bad.append('run: the program may not point outside the package')
    req = m.get('requirements')
    if req is not None and (not isinstance(req, str) or req.startswith('/') or '..' in req.split('/')):
        bad.append('requirements: a path inside the package, for example requirements.txt')
    env = m.get('env') or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and ENV_KEY_RE.match(k) and isinstance(v, (str, int, float))
                                            and '\n' not in str(v) and '"' not in str(v) and '\\' not in str(v)
                                            for k, v in env.items()):
        bad.append('env: {"KEY": "value"}, keys in CAPITALS, no quotes, backslashes or new lines in values')
    health = m.get('health') or {}
    if not isinstance(health, dict) or set(health) - {'within_s', 'http', 'cmd'}:
        bad.append('health: {"within_s": 20, "http": "http://127.0.0.1:PORT/path"} or {"cmd": "..."}')
    else:
        w = health.get('within_s', 20)
        if not isinstance(w, int) or not 5 <= w <= 300:
            bad.append('health.within_s: 5 to 300 seconds')
        url = health.get('http')
        if url is not None and not (isinstance(url, str) and re.match(r'^http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?(/[^\s]*)?$', url)):
            bad.append('health.http: a local address, http://127.0.0.1:PORT/path')
        cmd = health.get('cmd')
        if cmd is not None and not (isinstance(cmd, str) and all(ARG_RE.match(a) for a in cmd.split()) and cmd.split()):
            bad.append('health.cmd: a command (letters, digits and ./:=,@+%-_ only)')
    sa = m.get('stable_after_s', 600)
    if not isinstance(sa, int) or not 30 <= sa <= 86400:
        bad.append('stable_after_s: 30 to 86400 seconds')
    groups = m.get('groups') or []
    if not isinstance(groups, list) or any(g not in GROUPS for g in groups):
        bad.append('groups: any of ' + ', '.join(GROUPS))
    ports = m.get('ports') or []
    if not isinstance(ports, list) or not all(isinstance(p, int) and 1 <= p <= 65535 for p in ports):
        bad.append('ports: a list of port numbers')
    if m.get('memory_max') is not None and not (isinstance(m['memory_max'], str) and SIZE_RE.match(m['memory_max'])):
        bad.append('memory_max: like 256M or 1G')
    if m.get('cpu_quota') is not None and not (isinstance(m['cpu_quota'], str) and PCT_RE.match(m['cpu_quota'])):
        bad.append('cpu_quota: like 50%')
    rs = m.get('restart_sec', 5)
    if not isinstance(rs, int) or not 1 <= rs <= 300:
        bad.append('restart_sec: 1 to 300 seconds')
    if m.get('description') is not None and not isinstance(m['description'], str):
        bad.append('description: text')
    if bad:
        raise Fail('manifest.json: ' + '; '.join(bad))
    out = dict(m)
    out['run'] = run
    out['env'] = {k: str(v) for k, v in env.items()}
    out['health'] = dict(health, within_s=health.get('within_s', 20))
    out['stable_after_s'], out['groups'], out['ports'], out['restart_sec'] = sa, groups, ports, rs
    out['description'] = (m.get('description') or '')[:300]
    return out


def _member_ok(t):
    n = t.name
    if n.startswith('/') or '\\' in n or '..' in n.split('/') or n.startswith('./..'):
        raise Fail('package: unsafe path %r' % n)
    if not (t.isfile() or t.isdir()):
        raise Fail('package: %r is not a plain file or folder (links and devices are not allowed)' % n)


def read_package(path):
    """(manifest, sha256) of a package, after checking every member."""
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    try:
        with tarfile.open(path, 'r:gz') as tf:
            members, total, manifest = 0, 0, None
            for t in tf:
                members += 1
                total += max(t.size, 0)
                if members > MAX_MEMBERS or total > MAX_BYTES:
                    raise Fail('package: more than %d files or 1 GB unpacked' % MAX_MEMBERS)
                _member_ok(t)
                if t.name in ('manifest.json', './manifest.json') and t.isfile():
                    manifest = json.loads(tf.extractfile(t).read().decode('utf-8'))
    except (tarfile.TarError, OSError, EOFError):
        raise Fail('package: not a valid .esapp file (a gzip tarball made by esapp pack)')
    except ValueError:
        raise Fail('manifest.json is not valid JSON')
    if manifest is None:
        raise Fail('package: manifest.json missing at the top level')
    m = check_manifest(manifest)
    return m, h.hexdigest()


def _norm(name):
    return name[2:] if name.startswith('./') else name


def extract(path, dest):
    """Unpack a checked package into dest (which must not exist yet)."""
    os.makedirs(dest)
    with tarfile.open(path, 'r:gz') as tf:
        for t in tf:
            _member_ok(t)
            rel = _norm(t.name).rstrip('/')
            if not rel or rel == '.':
                continue
            target = os.path.join(dest, rel)
            if not os.path.realpath(target).startswith(os.path.realpath(dest) + os.sep):
                raise Fail('package: unsafe path %r' % t.name)
            if t.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tf.extractfile(t) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o700 if t.mode & 0o111 else 0o600)   # opened to the app's group by share_tree()


def pack(folder, out=None):
    """Build NAME-VERSION.esapp from a folder with manifest.json. Returns the path."""
    with open(os.path.join(folder, 'manifest.json'), encoding='utf-8') as fh:
        m = check_manifest(json.load(fh))
    out = out or os.path.join(os.getcwd(), '%s-%s%s' % (m['name'], m['version'], EXT))
    with tarfile.open(out, 'w:gz') as tf:
        for base, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(base, d)))
            for f in sorted(files):
                p = os.path.join(base, f)
                if f.endswith('.pyc') or os.path.islink(p) or os.path.abspath(p) == os.path.abspath(out):
                    continue
                rel = os.path.relpath(p, folder).replace(os.sep, '/')
                ti = tf.gettarinfo(p, rel)
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = 'root'
                with open(p, 'rb') as fh:
                    tf.addfile(ti, fh)
    read_package(out)                                   # the same checks a node will make
    return out


# ------------------------------------------------------------------ the node (one place for system calls)
def sh(args, check=True, timeout=600, env=None):
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                       universal_newlines=True, env=env)
    if check and r.returncode != 0:
        raise Fail('%s failed: %s' % (args[0], (r.stdout or '').strip()[-400:]))
    return r


def systemctl(*args, check=True):
    return sh(['systemctl'] + list(args), check=check, timeout=120)


def unit_state(unit):
    """(ActiveState, NRestarts)."""
    r = systemctl('show', unit, '-p', 'ActiveState', '-p', 'NRestarts', check=False)
    kv = dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)
    try:
        restarts = int(kv.get('NRestarts') or 0)
    except ValueError:
        restarts = 0
    return kv.get('ActiveState', 'unknown'), restarts


def user_exists(user):
    return sh(['id', '-u', user], check=False).returncode == 0


def ensure_user(user, home, groups):
    """A system user for the app; True when esapp created it."""
    created = False
    if not user_exists(user):
        sh(['useradd', '--system', '--user-group', '--home-dir', home, '--no-create-home',
            '--shell', '/usr/sbin/nologin', user])
        created = True
    have = [g for g in groups if sh(['getent', 'group', g], check=False).returncode == 0]
    if have:
        sh(['usermod', '-a', '-G', ','.join(have), user])
    return created


def remove_user(user):
    if user_exists(user):
        sh(['userdel', user], check=False)


def share_tree(path, group):
    """root owns it, the app's group may read (and enter, and run what is executable), others nothing."""
    sh(['chown', '-R', 'root:%s' % group, path])
    sh(['chmod', '-R', 'u=rwX,g=rX,o=', path])


def chown_tree(path, owner):
    sh(['chown', '-R', owner, path])


def run_as(user, args, cwd, timeout=900):
    return sh(['runuser', '-u', user, '--'] + args, check=False, timeout=timeout,
              env=dict(os.environ, HOME=cwd, PIP_NO_CACHE_DIR='1', PIP_DISABLE_PIP_VERSION_CHECK='1'))


def has_systemd():
    return os.path.isdir('/run/systemd/system') and shutil.which('systemctl') is not None


def ensure_venv_support():
    """python3 -m venv needs ensurepip; on Debian that is the python3-venv package."""
    if sh([sys.executable, '-c', 'import ensurepip, venv'], check=False).returncode == 0:
        return
    if not shutil.which('apt-get'):
        raise Fail('Python venv support is missing (install python3-venv)')
    sh(['apt-get', 'install', '-y', '-q', 'python3-venv'], timeout=900,
       env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'))
    record_system('apt', 'python3-venv')


def record_system(kind, value):
    """Shared system changes (never undone by one app's removal): kept for the audit."""
    path = os.path.join(ROOT, 'system.json')
    data = _load(path, {})
    data.setdefault(kind, [])
    if value not in data[kind]:
        data[kind].append(value)
    _save(path, data)


# ------------------------------------------------------------------ state
def now():
    return int(time.time())


def _load(path, default):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


class App:
    def __init__(self, name):
        if not NAME_RE.match(name or ''):
            raise Fail('not an app name: %r' % name)
        self.name = name
        self.dir = os.path.join(ROOT, 'apps', name)
        self.unit = 'es-%s.service' % name
        self.user = 'es-%s' % name
        self.state_path = os.path.join(self.dir, 'state.json')
        self.state = _load(self.state_path, {'name': name, 'versions': {}, 'current': None})

    def exists(self):
        return os.path.isdir(self.dir)

    def vdir(self, v):
        return os.path.join(self.dir, 'versions', v)

    def save(self):
        _save(self.state_path, self.state)

    def log(self, text):
        line = '%s %s' % (time.strftime('%Y-%m-%dT%H:%M:%S'), text)
        print(text, flush=True)
        try:
            with open(os.path.join(self.dir, 'esapp.log'), 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
        except OSError:
            pass
        hist = self.state.setdefault('history', [])
        hist.append(line[:300])
        del hist[:-50]

    def current(self):
        link = os.path.join(self.dir, 'current')
        if not os.path.islink(link):
            return None
        return os.path.basename(os.readlink(link))

    def manifest(self, v):
        return _load(os.path.join(self.vdir(v), 'manifest.json'), {})

    def stable(self):
        """Stable versions, newest first."""
        vs = [(info.get('stable_at') or 0, v) for v, info in self.state['versions'].items()
              if info.get('status') == 'stable' and os.path.isdir(self.vdir(v))]
        return [v for _, v in sorted(vs, reverse=True)]

    def refresh(self):
        """Promote the running version to stable once it has stayed up long enough."""
        cur = self.current()
        info = self.state['versions'].get(cur or '')
        if not cur or not info or info.get('status') != 'active':
            return
        m = self.manifest(cur)
        if now() - (info.get('activated_at') or now()) < m.get('stable_after_s', 600):
            return
        active, restarts = unit_state(self.unit)
        if active == 'active' and restarts <= info.get('restarts_at_start', 0):
            info['status'], info['stable_at'] = 'stable', now()
            self.log('%s is stable (up %ds without a restart)' % (cur, now() - info['activated_at']))
            self.save()


# ------------------------------------------------------------------ unit and scripts
def unit_text(app, m):
    cur = os.path.join(app.dir, 'current')
    run = list(m['run'])
    prog = run[0]
    if re.match(r'^python3?$', prog):
        prog = os.path.join(cur, 'venv', 'bin', 'python') if m.get('_venv') else '/usr/bin/python3'
    elif not prog.startswith('/'):
        prog = os.path.join(cur, prog)
    lines = ['[Unit]',
             'Description=EdgeShell app %s %s (managed by esapp; do not edit)' % (m['name'], m['version']),
             'After=network-online.target', 'Wants=network-online.target',
             'StartLimitIntervalSec=300', 'StartLimitBurst=5', '',
             '[Service]', 'Type=simple',
             'User=%s' % app.user, 'Group=%s' % app.user,
             'WorkingDirectory=%s' % cur,
             'Environment=APP_NAME=%s' % m['name'], 'Environment=APP_VERSION=%s' % m['version'],
             'Environment=APP_DATA=%s' % os.path.join(app.dir, 'data'),
             'Environment=PYTHONUNBUFFERED=1']
    lines += ['Environment="%s=%s"' % (k, v.replace('%', '%%').replace('$', '$$')) for k, v in sorted(m['env'].items())]
    lines += ['ExecStart=%s' % ' '.join([prog] + run[1:]).replace('%', '%%').replace('$', '$$'),
              'Restart=on-failure', 'RestartSec=%d' % m['restart_sec'],
              'NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes', 'PrivateTmp=yes',
              'ProtectKernelTunables=yes', 'ProtectControlGroups=yes', 'RestrictSUIDSGID=yes',
              'ReadWritePaths=%s' % os.path.join(app.dir, 'data'),
              'StandardOutput=journal', 'StandardError=journal', 'SyslogIdentifier=%s' % app.user]
    if m.get('memory_max'):
        lines.append('MemoryMax=%s' % m['memory_max'])
    if m.get('cpu_quota'):
        lines.append('CPUQuota=%s' % m['cpu_quota'])
    lines += ['', '[Install]', 'WantedBy=multi-user.target', '']
    return '\n'.join(lines)


SCRIPT = '''#!/bin/sh
# %(what)s for %(name)s %(version)s. Runs the esapp copy kept with this version,
# so it works even if the node's esapp changes later.
set -e
exec python3 "$(dirname "$0")/.esapp" %(args)s "$@"
'''


def write_scripts(app, v, vd):
    shutil.copyfile(os.path.abspath(__file__), os.path.join(vd, '.esapp'))
    for fname, what, args in (
            ('install.sh', 'Switch back to this version (it is already built)', 'activate %s %s' % (app.name, v)),
            ('rollback.sh', 'Go back to the newest stable version before the running one', 'rollback %s' % app.name),
            ('uninstall.sh', 'Remove this app completely (all versions, its service, user and data)',
             'uninstall %s' % app.name)):
        with open(os.path.join(vd, fname), 'w') as fh:
            fh.write(SCRIPT % {'what': what, 'name': app.name, 'version': v, 'args': args})
        os.chmod(os.path.join(vd, fname), 0o700)           # for root: they switch, roll back or remove the app


def install_unit(app, v):
    """Put version v's unit in place atomically and reload systemd."""
    src = os.path.join(app.vdir(v), app.unit)
    dst = os.path.join(UNIT_DIR, app.unit)
    tmp = dst + '.esapp-new'
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o600)                                # systemd reads units as root
    os.replace(tmp, dst)
    systemctl('daemon-reload')


def point_current(app, v):
    link = os.path.join(app.dir, 'current')
    tmp = os.path.join(app.dir, '.current-new')
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(os.path.join('versions', v), tmp)
    os.replace(tmp, link)                              # atomic: current is always one whole version


# ------------------------------------------------------------------ health
def healthy(app, m, baseline):
    """Watch the service for health.within_s seconds. (ok, reason)"""
    h = m.get('health') or {}
    window = h.get('within_s', 20)
    deadline = time.time() + window
    http_ok = cmd_ok = None
    while time.time() < deadline:
        time.sleep(1)
        active, restarts = unit_state(app.unit)
        if active == 'failed':
            return False, 'the service failed to start'
        if restarts > baseline:
            return False, 'the service crashed and restarted'
        if active == 'active':
            if h.get('http') and not http_ok:
                try:
                    with urllib.request.urlopen(h['http'], timeout=3) as r:  # nosec B310 - a local http URL checked in the manifest
                        http_ok = 200 <= r.status < 400
                except Exception:
                    http_ok = False
            if h.get('cmd') and not cmd_ok:
                cmd_ok = run_as(app.user, h['cmd'].split(), os.path.join(app.dir, 'current'), timeout=30).returncode == 0
    active, restarts = unit_state(app.unit)
    if active != 'active':
        return False, 'the service is %s after %ds' % (active, window)
    if restarts > baseline:
        return False, 'the service crashed and restarted'
    if h.get('http') and not http_ok:
        return False, 'health check %s did not answer' % h['http']
    if h.get('cmd') and not cmd_ok:
        return False, 'health check command did not succeed'
    return True, 'up for %ds%s' % (window, ', health check passed' if h.get('http') or h.get('cmd') else '')


# ------------------------------------------------------------------ commands
def need_root():
    if os.environ.get('ESAPP_ROOT') is None and os.geteuid() != 0:
        raise Fail('run as root: sudo esapp ...')


def need_systemd():
    if os.environ.get('ESAPP_ROOT') is None and not has_systemd():
        raise Fail('this node has no systemd; esapp apps run as systemd services')


def bootstrap():
    need_root()
    os.makedirs(os.path.join(ROOT, 'bin'), exist_ok=True)
    os.makedirs(os.path.join(ROOT, 'apps'), exist_ok=True)
    os.chmod(os.path.join(ROOT, 'bin'), 0o700)          # esapp is for root only
    for d in (ROOT, os.path.join(ROOT, 'apps')):
        sh(['chmod', 'u=rwx,go=x', d])                  # app users may pass through, not list or read
    dst = os.path.join(ROOT, 'bin', 'esapp')
    tmp = dst + '.new'
    shutil.copyfile(os.path.abspath(__file__), tmp)
    os.chmod(tmp, 0o700)
    os.replace(tmp, dst)
    return {'ok': True, 'esapp': ESAPP_VERSION, 'path': dst}


def install(pkg, sha=None, activate_now=True):
    need_root()
    need_systemd()
    m, digest = read_package(pkg)
    if sha and sha.lower() != digest:
        raise Fail('package checksum does not match what EdgeShell sent (%s)' % digest[:12])
    app = App(m['name'])
    v = m['version']
    os.makedirs(os.path.join(app.dir, 'ota'), exist_ok=True)
    os.makedirs(os.path.join(app.dir, 'versions'), exist_ok=True)
    for d in (app.dir, os.path.join(app.dir, 'ota'), os.path.join(app.dir, 'versions')):
        os.chmod(d, 0o700)                              # root only until the app's user exists
    have = app.state['versions'].get(v)
    if have and have.get('sha256') != digest:
        raise Fail('%s %s was installed here with different contents; give the new build a new version' % (app.name, v))
    if have and have.get('status') == 'failed':
        raise Fail('%s %s failed here before (%s); give the fixed build a new version'
                   % (app.name, v, have.get('failed_reason') or 'rolled back'))
    if have and os.path.isdir(app.vdir(v)):
        _drop_staged(pkg)
        if app.current() == v:
            return {'ok': True, 'app': app.name, 'version': v, 'note': 'already running'}
        return activate(app.name, v) if activate_now else {'ok': True, 'app': app.name, 'version': v}
    # Keep the package as received (ota/), then build the version beside the running one
    ota = os.path.join(app.dir, 'ota', '%s-%s%s' % (app.name, v, EXT))
    shutil.copyfile(pkg, ota + '.tmp')
    os.replace(ota + '.tmp', ota)
    _drop_staged(pkg)
    app.log('received %s %s (%s)' % (app.name, v, digest[:12]))
    created = ensure_user(app.user, os.path.join(app.dir, 'data'), m['groups'])
    app.state.setdefault('created_user', created)       # uninstall removes the user only if esapp made it
    for d in (app.dir, os.path.join(app.dir, 'versions')):
        sh(['chown', 'root:%s' % app.user, d])
        sh(['chmod', 'u=rwx,g=x,o=', d])               # the app's user may pass through, nobody else
    data = os.path.join(app.dir, 'data')
    if not os.path.isdir(data):
        os.makedirs(data)
        chown_tree(data, '%s:%s' % (app.user, app.user))
        os.chmod(data, 0o700)                           # the app's own files: its user only
    build = os.path.join(app.dir, 'versions', '.build-%s' % v)
    if os.path.lexists(build):
        shutil.rmtree(build)
    try:
        extract(ota, build)
        m['_venv'] = bool(m.get('requirements')) or bool(re.match(r'^python3?$', m['run'][0]))
        if m['_venv']:
            _venv(app, m, build)
        with open(os.path.join(build, 'manifest.json'), 'w') as fh:
            json.dump(m, fh, indent=1, sort_keys=True)
        with open(os.path.join(build, app.unit), 'w') as fh:
            fh.write(unit_text(app, m))
        write_scripts(app, v, build)
        share_tree(build, app.user)                     # read-only for the app, invisible to others
        os.rename(build, app.vdir(v))
    except Exception:
        shutil.rmtree(build, ignore_errors=True)
        raise
    app.state['versions'][v] = {'status': 'installed', 'sha256': digest, 'installed_at': now(),
                                'ota': os.path.basename(ota)}
    app.log('built %s %s' % (app.name, v))
    app.save()
    if not activate_now:
        return {'ok': True, 'app': app.name, 'version': v, 'note': 'installed, not running'}
    return activate(app.name, v)


def _drop_staged(pkg):
    """Packages are staged in ~/Downloads/edgeshell; once kept in ota/ the staged copy goes."""
    if '/Downloads/edgeshell/' in os.path.abspath(pkg).replace(os.sep, '/'):
        try:
            os.remove(pkg)
        except OSError:
            pass


def _venv(app, m, build):
    """The version's own Python environment, packages installed as the app user (never as root)."""
    ensure_venv_support()
    chown_tree(build, '%s:%s' % (app.user, app.user))
    venv = os.path.join(build, 'venv')
    r = run_as(app.user, [sys.executable, '-m', 'venv', venv], build)
    if r.returncode != 0:
        raise Fail('could not create the venv: %s' % r.stdout.strip()[-300:])
    req = m.get('requirements')
    if req:
        args = [os.path.join(venv, 'bin', 'pip'), 'install', '-q', '-r', os.path.join(build, req)]
        if os.path.isdir(os.path.join(build, 'wheels')):
            args += ['--no-index', '--find-links', os.path.join(build, 'wheels')]   # offline: wheels in the package
        r = run_as(app.user, args, build)
        if r.returncode != 0:
            raise Fail('pip could not install %s: %s' % (req, r.stdout.strip()[-400:]))
    # Paths inside a venv are absolute: point them at the final folder, not the build folder
    final = app.vdir(m['version'])
    for base, _, files in os.walk(os.path.join(venv, 'bin')):
        for f in files:
            p = os.path.join(base, f)
            if os.path.islink(p):
                continue
            try:
                with open(p, 'r', encoding='utf-8') as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            if build in text:
                with open(p, 'w', encoding='utf-8') as fh:
                    fh.write(text.replace(build, final))
    cfg = os.path.join(venv, 'pyvenv.cfg')
    if os.path.isfile(cfg):
        with open(cfg) as fh:
            text = fh.read()
        with open(cfg, 'w') as fh:
            fh.write(text.replace(build, final))


def activate(name, v, _auto=True):
    """Switch to version v; if it does not stay healthy, switch back by itself."""
    need_root()
    need_systemd()
    app = App(name)
    if v not in app.state['versions'] or not os.path.isdir(app.vdir(v)):
        raise Fail('%s %s is not installed here' % (name, v))
    prev = app.current()
    if prev == v:
        return {'ok': True, 'app': name, 'version': v, 'note': 'already running'}
    m = app.manifest(v)
    app.log('switching %s from %s to %s' % (name, prev or 'nothing', v))
    point_current(app, v)
    install_unit(app, v)
    systemctl('enable', app.unit, check=False)
    systemctl('reset-failed', app.unit, check=False)
    systemctl('restart', app.unit, check=False)
    _, baseline = unit_state(app.unit)
    ok, why = healthy(app, m, baseline)
    info = app.state['versions'][v]
    if ok:
        # a version that earned 'stable' keeps it when it comes back (a rollback target)
        info.update(status='stable' if info.get('status') == 'stable' else 'active',
                    activated_at=now(), restarts_at_start=baseline)
        info.pop('failed_reason', None)
        if prev and prev in app.state['versions']:
            app.state['previous'] = prev
        app.state['current'] = v
        app.log('%s %s is running (%s)' % (name, v, why))
        app.save()
        return {'ok': True, 'app': name, 'version': v, 'previous': prev, 'health': why}
    info.update(status='failed', failed_at=now(), failed_reason=why)
    app.log('%s %s is not healthy: %s' % (name, v, why))
    back = None
    if _auto:
        back = _switch_back(app, prev, v)
    app.save()
    return {'ok': False, 'app': name, 'version': v, 'error': why, 'rolled_back_to': back}


def _switch_back(app, prev, failed):
    """Put the version that ran before back (or stop the app if there was none)."""
    if prev and os.path.isdir(app.vdir(prev)):
        point_current(app, prev)
        install_unit(app, prev)
        systemctl('reset-failed', app.unit, check=False)
        systemctl('restart', app.unit, check=False)
        _, baseline = unit_state(app.unit)
        ok, why = healthy(app, app.manifest(prev), baseline)
        app.state['current'] = prev
        info = app.state['versions'].get(prev, {})
        info['restarts_at_start'] = baseline
        app.log('rolled back to %s (%s)' % (prev, why if ok else 'NOT healthy either: ' + why))
        return prev
    systemctl('disable', '--now', app.unit, check=False)
    link = os.path.join(app.dir, 'current')
    if os.path.islink(link):
        os.remove(link)
    app.state['current'] = None
    app.log('no earlier version of %s to go back to: stopped it (%s failed)' % (app.name, failed))
    return None


def rollback(name, to=None, keep_failed=False):
    """One click: back to the newest stable version before the running one; the
    version rolled back from is marked failed and removed (its OTA copy stays)."""
    need_root()
    need_systemd()
    app = App(name)
    if not app.exists():
        raise Fail('%s is not installed here' % name)
    app.refresh()
    cur = app.current()
    if to is None:
        choices = [v for v in app.stable() if v != cur]
        if not choices:
            raise Fail('%s has no other stable version to go back to' % name)
        to = choices[0]
    if to == cur:
        raise Fail('%s %s is already running' % (name, to))
    r = activate(name, to, _auto=True)
    if not r['ok']:
        return r
    app = App(name)
    if cur:
        info = app.state['versions'].get(cur, {})
        info.update(status='failed', failed_at=now(), failed_reason=info.get('failed_reason') or 'rolled back')
        if not keep_failed:
            shutil.rmtree(app.vdir(cur), ignore_errors=True)
            info['removed_at'] = now()
        app.log('rolled back %s from %s to %s%s' % (name, cur, to, '' if keep_failed else '; removed ' + cur))
        app.save()
    return {'ok': True, 'app': name, 'version': to, 'removed': None if keep_failed else cur}


def protected(app):
    """Versions prune never removes: the running one and the newest MIN_STABLE stable ones."""
    keep = set(app.stable()[:MIN_STABLE])
    if app.current():
        keep.add(app.current())
    return keep


def prune(name, versions=None):
    need_root()
    app = App(name)
    if not app.exists():
        raise Fail('%s is not installed here' % name)
    app.refresh()
    keep = protected(app)
    known = set(app.state['versions'])
    wanted = set(versions) if versions else known - keep
    removed, kept = [], sorted(wanted & keep)
    for v in sorted(wanted - keep):
        if v not in known:
            continue
        info = app.state['versions'][v]
        shutil.rmtree(app.vdir(v), ignore_errors=True)
        ota = os.path.join(app.dir, 'ota', info.get('ota') or '')
        if info.get('ota') and os.path.isfile(ota):
            os.remove(ota)
        del app.state['versions'][v]
        removed.append(v)
    if removed:
        app.log('pruned %s: %s' % (name, ', '.join(removed)))
    app.save()
    return {'ok': True, 'app': name, 'removed': removed, 'kept_protected': kept,
            'note': 'the running version and the newest %d stable versions are always kept' % MIN_STABLE}


def uninstall(name, keep_data=False):
    """Remove the app completely: service, unit, versions, OTA copies, user, data."""
    need_root()
    app = App(name)
    unit_path = os.path.join(UNIT_DIR, app.unit)
    if not app.exists() and not os.path.exists(unit_path):
        raise Fail('%s is not installed here' % name)
    apps_root = os.path.realpath(os.path.join(ROOT, 'apps'))
    if not os.path.realpath(app.dir).startswith(apps_root + os.sep):
        raise Fail('refusing to remove a folder outside %s' % apps_root)
    systemctl('disable', '--now', app.unit, check=False)
    if os.path.exists(unit_path):
        os.remove(unit_path)
    systemctl('daemon-reload', check=False)
    systemctl('reset-failed', app.unit, check=False)
    kept = None
    if keep_data and os.path.isdir(os.path.join(app.dir, 'data')):
        kept = os.path.join(ROOT, 'kept', '%s-data-%s' % (name, time.strftime('%Y%m%d-%H%M%S')))
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        os.rename(os.path.join(app.dir, 'data'), kept)
        chown_tree(kept, 'root:root')
        os.chmod(kept, 0o700)
    created = app.state.get('created_user', False)
    shutil.rmtree(app.dir, ignore_errors=True)
    if created:
        remove_user(app.user)
    return {'ok': True, 'app': name, 'removed': True, 'data_kept_in': kept}


def inventory():
    apps = []
    base = os.path.join(ROOT, 'apps')
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if not NAME_RE.match(name):
            continue
        app = App(name)
        try:
            app.refresh()
        except Exception:                               # noqa: BLE001 - one app never hides the others
            pass
        active, restarts = unit_state(app.unit)
        keep = protected(app)
        versions = []
        for v, info in sorted(app.state['versions'].items(), key=lambda kv: kv[1].get('installed_at') or 0, reverse=True):
            ota = os.path.join(app.dir, 'ota', info.get('ota') or '')
            versions.append({'version': v, 'status': info.get('status'), 'installed_at': info.get('installed_at'),
                             'activated_at': info.get('activated_at'), 'stable_at': info.get('stable_at'),
                             'failed_reason': info.get('failed_reason'), 'sha256': info.get('sha256'),
                             'on_disk': os.path.isdir(app.vdir(v)), 'protected': v in keep,
                             'size': _du(app.vdir(v)) + (os.path.getsize(ota) if info.get('ota') and os.path.isfile(ota) else 0)})
        m = app.manifest(app.current()) if app.current() else {}
        apps.append({'name': name, 'current': app.current(), 'service': active, 'restarts': restarts,
                     'unit': app.unit, 'ports': m.get('ports') or [], 'description': m.get('description') or '',
                     'versions': versions, 'history': app.state.get('history', [])[-10:]})
    return {'ok': True, 'esapp': ESAPP_VERSION, 'root': ROOT, 'apps': apps,
            'system': _load(os.path.join(ROOT, 'system.json'), {})}


def _du(path):
    total = 0
    for base, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(base, f)).st_size
            except OSError:
                pass
    return total


def status(name):
    app = App(name)
    if not app.exists():
        raise Fail('%s is not installed here' % name)
    return [a for a in inventory()['apps'] if a['name'] == name][0]


def logs(name, n=100):
    App(name)
    r = sh(['journalctl', '-u', 'es-%s.service' % name, '-n', str(max(1, min(int(n), 2000))), '--no-pager'], check=False)
    return {'ok': True, 'app': name, 'lines': r.stdout.splitlines()[-2000:]}


# ------------------------------------------------------------------ command line
def main(argv=None):
    ap = argparse.ArgumentParser(prog='esapp', description='EdgeShell app manager (versions in /opt/edgeshell).')
    sub = ap.add_subparsers(dest='cmd')
    p = sub.add_parser('pack'); p.add_argument('folder'); p.add_argument('-o', '--out')
    p = sub.add_parser('check'); p.add_argument('package')
    sub.add_parser('bootstrap')
    sub.add_parser('version')
    p = sub.add_parser('install'); p.add_argument('package'); p.add_argument('--sha256'); p.add_argument('--no-activate', action='store_true')
    p = sub.add_parser('activate'); p.add_argument('name'); p.add_argument('version')
    p = sub.add_parser('rollback'); p.add_argument('name'); p.add_argument('--to'); p.add_argument('--keep-failed', action='store_true')
    p = sub.add_parser('prune'); p.add_argument('name'); p.add_argument('--versions')
    p = sub.add_parser('uninstall'); p.add_argument('name'); p.add_argument('--keep-data', action='store_true')
    sub.add_parser('inventory')
    p = sub.add_parser('status'); p.add_argument('name')
    p = sub.add_parser('logs'); p.add_argument('name'); p.add_argument('-n', type=int, default=100)
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'pack':
            out = pack(a.folder, a.out)
            m, digest = read_package(out)
            res = {'ok': True, 'package': out, 'name': m['name'], 'version': m['version'], 'sha256': digest}
        elif a.cmd == 'check':
            m, digest = read_package(a.package)
            res = {'ok': True, 'manifest': m, 'sha256': digest}
        elif a.cmd == 'bootstrap':
            res = bootstrap()
        elif a.cmd == 'version':
            res = {'ok': True, 'esapp': ESAPP_VERSION}
        elif a.cmd == 'install':
            res = install(a.package, a.sha256, not a.no_activate)
        elif a.cmd == 'activate':
            res = activate(a.name, a.version)
        elif a.cmd == 'rollback':
            res = rollback(a.name, a.to, a.keep_failed)
        elif a.cmd == 'prune':
            res = prune(a.name, [v.strip() for v in a.versions.split(',') if v.strip()] if a.versions else None)
        elif a.cmd == 'uninstall':
            res = uninstall(a.name, a.keep_data)
        elif a.cmd == 'inventory':
            res = inventory()
        elif a.cmd == 'status':
            res = status(a.name)
            res['ok'] = True
        elif a.cmd == 'logs':
            res = logs(a.name, a.n)
            print('\n'.join(res['lines']))
        else:
            ap.print_help()
            return 2
    except Fail as e:
        res = {'ok': False, 'error': str(e)}
        print('esapp: %s' % e, file=sys.stderr)
    print('ESAPP_RESULT ' + json.dumps(res, sort_keys=True))
    return 0 if res.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
