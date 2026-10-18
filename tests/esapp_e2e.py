"""esapp on a real systemd node (Debian 12 in a container): versions side by
side in /opt/edgeshell, its own venv per version, a crashing version rolls
itself back, one-click rollback, cleanup never below three stable versions,
and a full uninstall that leaves the system as it was.

  docker run -d --name esnode --privileged --cgroupns=host \
      -v /sys/fs/cgroup:/sys/fs/cgroup:rw jrei/systemd-debian:12
  python tests/esapp_e2e.py --node esnode

Exit code 0 = every check passed. Prints a STATS line for the docs.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ESAPP = os.path.join(HERE, '..', 'app', 'agents', 'esapp', 'esapp.py')
sys.path.insert(0, os.path.dirname(ESAPP))
import esapp  # noqa: E402

APP = '''import http.server, os, sys
MODE = %r
if MODE == 'crash':
    sys.exit(3)
data = os.environ['APP_DATA']
open(os.path.join(data, 'seen-' + os.environ['APP_VERSION']), 'w').write('ok')
try:
    import six  # from this version's own venv
    lib = 'six ' + six.__version__
except ImportError:
    lib = 'no six'
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ('%%s %%s' %% (os.environ['APP_VERSION'], lib)).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', 18080), H).serve_forever()
'''

checks = []


def check(ok, what, detail=''):
    checks.append(ok)
    print(('PASS ' if ok else 'FAIL ') + what + (('  [' + str(detail)[:300] + ']') if detail and not ok else ''), flush=True)
    return ok


def dx(node, cmd, check_rc=False):
    r = subprocess.run(['docker', 'exec', node, 'bash', '-lc', cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True)
    if check_rc and r.returncode:
        raise SystemExit('command failed: %s\n%s' % (cmd, r.stdout))
    return r


def esapp_on(node, args):
    r = dx(node, 'python3 /opt/edgeshell/bin/esapp ' + args)
    last = [l for l in r.stdout.splitlines() if l.startswith('ESAPP_RESULT ')]
    res = json.loads(last[-1][len('ESAPP_RESULT '):]) if last else {'ok': False, 'error': r.stdout[-500:]}
    return res, r.stdout


def build(tmp, version, mode='ok', six=True, stable_after=30):
    d = os.path.join(tmp, 'src-' + version)
    os.makedirs(os.path.join(d, 'app'))
    with open(os.path.join(d, 'app', 'main.py'), 'w') as fh:
        fh.write(APP % mode)
    m = {'name': 'demo-sensor', 'version': version, 'description': 'esapp end to end',
         'run': 'python app/main.py', 'health': {'within_s': 12, 'http': 'http://127.0.0.1:18080/'},
         'stable_after_s': stable_after, 'env': {'GREETING': 'hello world'}, 'ports': [18080], 'memory_max': '128M'}
    if six:
        m['requirements'] = 'requirements.txt'
        with open(os.path.join(d, 'requirements.txt'), 'w') as fh:
            fh.write('six==1.16.0\n')
    with open(os.path.join(d, 'manifest.json'), 'w') as fh:
        json.dump(m, fh)
    return esapp.pack(d, os.path.join(tmp, 'demo-sensor-%s.esapp' % version))


def push(node, pkg):
    """As EdgeShell does: the package goes to ~/Downloads/edgeshell, esapp takes it from there."""
    dx(node, 'mkdir -p /root/Downloads/edgeshell', True)
    subprocess.run(['docker', 'cp', pkg, '%s:/root/Downloads/edgeshell/' % node], check=True)
    m, digest = esapp.read_package(pkg)
    t = time.time()
    res, out = esapp_on(node, 'install /root/Downloads/edgeshell/%s --sha256 %s' % (os.path.basename(pkg), digest))
    return res, out, time.time() - t


def snapshot(node):
    """Everything outside /opt/edgeshell that an app could leave behind."""
    return dx(node, 'find /etc/systemd/system -type f -o -type l | sort; getent passwd | cut -d: -f1 | sort; ls /usr/local/lib/python3*/dist-packages 2>/dev/null | sort; '
                    'python3 -c "import six" 2>&1 | tail -1').stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--node', required=True)
    a = ap.parse_args()
    node = a.node
    tmp = tempfile.mkdtemp()
    stats = {}
    dx(node, 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 curl >/dev/null', True)
    before = snapshot(node)
    subprocess.run(['docker', 'cp', ESAPP, '%s:/root/esapp.py' % node], check=True)
    r = dx(node, 'python3 /root/esapp.py bootstrap')
    check('ESAPP_RESULT' in r.stdout and dx(node, 'test -x /opt/edgeshell/bin/esapp').returncode == 0,
          'bootstrap puts esapp in /opt/edgeshell/bin', r.stdout)

    p1, p2bad, p2, p3, p4 = (build(tmp, '1.0.0'), build(tmp, '2.0.0', 'crash'), build(tmp, '2.0.1'),
                             build(tmp, '3.0.0'), build(tmp, '4.0.0'))
    bad = os.path.join(tmp, 'evil.esapp')
    import tarfile, io
    with tarfile.open(bad, 'w:gz') as tf:
        data = json.dumps({'name': 'evil-app', 'version': '1.0.0', 'run': 'python x.py'}).encode()
        ti = tarfile.TarInfo('manifest.json'); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
        ti = tarfile.TarInfo('../../etc/cron.d/x'); ti.size = 1; tf.addfile(ti, io.BytesIO(b'x'))
    try:
        esapp.read_package(bad)
        check(False, 'a package that writes outside its folder is refused')
    except esapp.Fail as e:
        check('unsafe path' in str(e), 'a package that writes outside its folder is refused', e)

    res, out, s = push(node, p1)
    stats['install_first_s'] = round(s, 1)
    check(res.get('ok') and res.get('version') == '1.0.0', 'first version installs, gets its venv and starts healthy', out[-800:])
    check(dx(node, 'curl -s 127.0.0.1:18080').stdout.startswith('1.0.0 six 1.16.0'), 'it serves from its own venv (six only in the app venv)')
    check(dx(node, 'test ! -e /root/Downloads/edgeshell/demo-sensor-1.0.0.esapp && test -f /opt/edgeshell/apps/demo-sensor/ota/demo-sensor-1.0.0.esapp').returncode == 0,
          'the staged package left ~/Downloads/edgeshell and is kept as the OTA copy')
    unit = dx(node, 'systemctl show es-demo-sensor -p User -p ProtectSystem -p NoNewPrivileges -p MemoryMax -p UnitFileState').stdout
    check('User=es-demo-sensor' in unit and 'ProtectSystem=strict' in unit and 'NoNewPrivileges=yes' in unit
          and 'UnitFileState=enabled' in unit, 'systemd service: own user, hardened, enabled at boot', unit)
    check(dx(node, 'ls /opt/edgeshell/apps/demo-sensor/versions/1.0.0/ | tr "\\n" " "').stdout.count('.sh') == 3,
          'the version keeps its own install.sh, rollback.sh and uninstall.sh')
    check(dx(node, 'su -s /bin/sh es-demo-sensor -c "touch /opt/edgeshell/apps/demo-sensor/current/x" 2>/dev/null').returncode != 0,
          'the app cannot change its own code (version folders are root-only)')
    check(dx(node, 'su -s /bin/sh nobody -c "cat /opt/edgeshell/apps/demo-sensor/current/app/main.py; ls /opt/edgeshell/apps" 2>/dev/null').returncode != 0
          and dx(node, 'stat -c %a /opt/edgeshell/apps/demo-sensor/data /opt/edgeshell/bin/esapp /etc/systemd/system/es-demo-sensor.service | tr "\n" " "').stdout.split() == ['700', '700', '600'],
          'other users cannot read the app, its data or esapp')
    check(dx(node, 'journalctl -u es-demo-sensor --no-pager | grep -c "Started"').stdout.strip() not in ('', '0'), 'logs go to the journal')

    res, out, s = push(node, p2bad)
    stats['auto_rollback_s'] = round(s, 1)
    check(not res.get('ok') and res.get('rolled_back_to') == '1.0.0', 'a crashing version rolls itself back', out[-800:])
    check(dx(node, 'curl -s 127.0.0.1:18080').stdout.startswith('1.0.0'), 'the earlier version serves again after the failed update')
    res, out = esapp_on(node, 'install /opt/edgeshell/apps/demo-sensor/ota/demo-sensor-2.0.0.esapp')
    check(not res.get('ok') and 'failed here before' in res.get('error', ''), 'the failed build is never installed again', res)

    # Make versions stable quickly: stable_after_s is 30 in these packages
    seq = []
    for pkg in (p2, p3, p4):
        time.sleep(31)
        esapp_on(node, 'inventory')                       # promotes the running version
        res, out, s = push(node, pkg)
        seq.append(res.get('ok'))
    check(all(seq), 'three more versions install one after another', seq)
    time.sleep(31)
    inv, _ = esapp_on(node, 'inventory')
    app = inv['apps'][0]
    st = {v['version']: v['status'] for v in app['versions']}
    check(st == {'1.0.0': 'stable', '2.0.0': 'failed', '2.0.1': 'stable', '3.0.0': 'stable', '4.0.0': 'stable'},
          'inventory shows each version and its state', st)

    res, out = esapp_on(node, 'prune demo-sensor --versions 1.0.0,2.0.0,2.0.1,3.0.0,4.0.0')
    check(res.get('ok') and res['removed'] == ['1.0.0', '2.0.0'] and set(res['kept_protected']) == {'2.0.1', '3.0.0', '4.0.0'},
          'cleanup removes what was picked but never the running or the newest 3 stable versions', res)
    check(dx(node, 'test ! -e /opt/edgeshell/apps/demo-sensor/versions/1.0.0 && test ! -e /opt/edgeshell/apps/demo-sensor/ota/demo-sensor-1.0.0.esapp').returncode == 0,
          'a pruned version leaves with its OTA copy')

    t = time.time()
    res, out = esapp_on(node, 'rollback demo-sensor')
    stats['one_click_rollback_s'] = round(time.time() - t, 1)
    check(res.get('ok') and res.get('version') == '3.0.0' and res.get('removed') == '4.0.0',
          'one-click rollback: back to the newest stable before, the one rolled back from removed', res)
    check(dx(node, 'curl -s 127.0.0.1:18080').stdout.startswith('3.0.0'), 'the rolled-back version serves')

    dx(node, 'systemctl restart es-demo-sensor')
    check(dx(node, 'ls /opt/edgeshell/apps/demo-sensor/data/').stdout.split() == ['seen-1.0.0', 'seen-2.0.1', 'seen-3.0.0', 'seen-4.0.0'],
          'the data folder is shared by every version and survives updates')

    t = time.time()
    r = dx(node, 'sh /opt/edgeshell/apps/demo-sensor/current/uninstall.sh')
    stats['uninstall_s'] = round(time.time() - t, 1)
    check('"removed": true' in r.stdout, "the version's own uninstall.sh removes the app", r.stdout[-400:])
    check(dx(node, 'test ! -e /opt/edgeshell/apps/demo-sensor && ! id es-demo-sensor 2>/dev/null && ! systemctl cat es-demo-sensor 2>/dev/null').returncode == 0,
          'nothing of the app is left: folder, user, service')
    after = snapshot(node)
    check(before == after, 'the system outside /opt/edgeshell is exactly as before (units, users, system Python packages)',
          '\n'.join(sorted(set(after.splitlines()) ^ set(before.splitlines()))))
    print('STATS esapp end to end on Debian 12 systemd', json.dumps(stats, sort_keys=True))
    print('%d of %d checks passed' % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


if __name__ == '__main__':
    sys.exit(main())
