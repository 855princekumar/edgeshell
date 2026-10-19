"""The heartbeat agent as an esapp app, on a real systemd node with a real broker
(ADR 0018). EdgeShell's own code builds the package, the token and the files;
they reach the node as they would over SSH (the package in ~/Downloads/edgeshell,
the rest in a private temp folder) and hb_node.sh runs as root.

  docker run -d --name mq -p 1883:1883 eclipse-mosquitto:2.0.20 mosquitto -c /mosquitto-no-auth.conf
  docker run -d --name hbnode --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
      --add-host host.docker.internal:host-gateway jrei/systemd-debian:12
  python tests/heartbeat_esapp_e2e.py --node hbnode --broker-container mq

Checks: a classic install moved over only once the new agent runs, and kept
when it does not; the agent's own venv (no system packages); private settings;
the agent's own test; a settings push; a bad update that rolls itself back.
Exit code 0 = every check passed. Prints a STATS line for the docs.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
os.environ.setdefault('FLASK_ENV', 'testing')

checks = []
NODE = 'e2e-hb-node'


def check(ok, what, detail=''):
    checks.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + what + (('  [' + str(detail)[-600:] + ']') if detail and not ok else ''), flush=True)
    return ok


def dx(node, cmd):
    return subprocess.run(['docker', 'exec', node, 'bash', '-c', cmd], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, encoding='utf-8', errors='replace')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--node', required=True)
    ap.add_argument('--broker-container', required=True)
    a = ap.parse_args()
    node, mq = a.node, a.broker_container
    stats = {}

    from app import create_app, db
    from app.models.node import Node
    from app.services import app_service, heartbeat_service as hb
    work = tempfile.mkdtemp()
    flask_app = create_app('testing')
    flask_app.config.update(HEARTBEAT_MASTER_KEY=os.urandom(32).hex(), UPLOAD_FOLDER=os.path.join(work, 'uploads'),
                            HEARTBEAT_BROKER_USERNAME='edge', HEARTBEAT_BROKER_PASSWORD='e2e-pass-1',
                            HEARTBEAT_BROKER='host.docker.internal', HEARTBEAT_PORT=1883, HEARTBEAT_TLS='false',
                            HEARTBEAT_INTERVAL=5)
    ctx = flask_app.app_context()
    ctx.push()
    db.create_all()
    n = Node(name=NODE, ip_address='127.0.0.1', username='root')

    def files():
        f = {name: hb._read(name) for name in hb.AGENT_FILES}
        f['token'], f['login'] = hb.join_token(n), hb.login_env()
        return f

    def stage(fs):
        """As EdgeShell's _upload: a private temp folder, files 0600."""
        d = dx(node, 'd=$(mktemp -d /tmp/edgeshell-hb.XXXXXXXX) && chmod 700 "$d" && echo "$d"').stdout.strip()
        for name, text in fs.items():
            p = os.path.join(work, name)
            with open(p, 'w', newline='\n') as fh:
                fh.write(text)
            subprocess.run(['docker', 'cp', p, '%s:%s/%s' % (node, d, name)], check=True)
        dx(node, 'chmod 600 %s/*' % d)
        return d

    def put(pkg_path, name):
        dx(node, 'mkdir -p /root/Downloads/edgeshell && chmod 750 /root/Downloads/edgeshell')
        subprocess.run(['docker', 'cp', pkg_path, '%s:/root/Downloads/edgeshell/%s' % (node, name)], check=True)
        return '/root/Downloads/edgeshell/' + name

    def install(pkg_path, version, sha):
        d = stage(files())
        remote = put(pkg_path, os.path.basename(pkg_path))
        t = time.time()
        r = dx(node, 'bash %s/hb_node.sh install %s %s %s 5; rc=$?; rm -rf %s; exit $rc' % (d, remote, sha, version, d))
        return r, time.time() - t

    def helper(mode, fs):
        d = stage(dict(fs, **{'hb_node.sh': hb._read('hb_node.sh')}))
        return dx(node, 'bash %s/hb_node.sh %s; rc=$?; rm -rf %s; exit $rc' % (d, mode, d))

    def heard(seconds=40):
        """A message from this node's topic reached the broker (it is encrypted; the topic is not the name)."""
        r = subprocess.run(['docker', 'exec', mq, 'mosquitto_sub', '-h', '127.0.0.1', '-t', '#', '-C', '1', '-W', str(seconds), '-v'],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace')
        return r.returncode == 0 and r.stdout.strip() != '', r.stdout[:200]

    def bad_package(version):
        src = os.path.join(work, 'bad-' + version)
        os.makedirs(os.path.join(src, 'app'))
        m = json.load(open(os.path.join(hb.AGENT_DIR, 'esapp.json')))
        m['version'] = version
        json.dump(m, open(os.path.join(src, 'manifest.json'), 'w'))
        shutil.copyfile(os.path.join(hb.AGENT_DIR, 'requirements.txt'), os.path.join(src, 'requirements.txt'))
        for name in ('heartbeat_protocol.py', 'setup.py'):
            shutil.copyfile(os.path.join(hb.AGENT_DIR, name), os.path.join(src, 'app', name))
        text = open(os.path.join(hb.AGENT_DIR, 'heartbeat_agent.py')).read()
        text = text.replace("if __name__ == '__main__':", "if __name__ == '__main__':\n    sys.exit('broken on purpose')", 1)
        open(os.path.join(src, 'app', 'heartbeat_agent.py'), 'w', newline='\n').write(text)
        out = app_service.ES.pack(src, os.path.join(work, 'heartbeat-%s.esapp' % version))
        return out, app_service.ES.read_package(out)[1]

    # ---------------------------------------------------------------- the node
    r = dx(node, 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 procps >/dev/null; echo ok')
    subprocess.run(['docker', 'cp', app_service.ESAPP_PATH, '%s:/root/esapp.py' % node], check=True)
    check('ESAPP_RESULT' in dx(node, 'python3 /root/esapp.py bootstrap && rm /root/esapp.py').stdout, 'esapp set up on the node')

    # 1. a classic install (as nodes have it today): apt packages, /opt/edgeshell-heartbeat, user hbagent
    d = stage(files())
    r = dx(node, 'cd %s && python3 setup.py --token-file token --login-file login --interval 5; rc=$?; rm -rf %s; exit $rc' % (d, d))
    check('HEARTBEAT_OK' in r.stdout and dx(node, 'systemctl is-active --quiet edgeshell-heartbeat').returncode == 0,
          'a classic install runs (the starting point of every node today)', r.stdout[-500:])

    # 2. a broken first esapp version: the classic agent must run again
    bad1, bad1_sha = bad_package('1.3.0-bad1')
    r, s = install(bad1, '1.3.0-bad1', bad1_sha)
    check(r.returncode != 0 and 'HEARTBEAT_FAILED' in r.stdout and 'The classic agent runs again' in r.stdout
          and dx(node, 'systemctl is-active --quiet edgeshell-heartbeat').returncode == 0
          and dx(node, 'test -d /opt/edgeshell-heartbeat').returncode == 0,
          'a new agent that does not start leaves the classic one running', r.stdout[-800:])

    # 3. the real move
    pkg = hb.agent_package()
    t0 = time.time()
    r, s = install(app_service.package_path(pkg), pkg.version, pkg.sha256)
    stats['move_from_classic_s'] = round(s, 1)
    check(r.returncode == 0 and 'HEARTBEAT_OK %s' % pkg.version in r.stdout and 'Done.' in r.stdout,
          'the agent moves to esapp (build, settings, switch)', r.stdout[-1200:])
    check(dx(node, 'systemctl is-active --quiet es-heartbeat && systemctl is-enabled --quiet es-heartbeat').returncode == 0,
          'service es-heartbeat running and enabled at boot')
    check(dx(node, 'test ! -e /opt/edgeshell-heartbeat && ! id hbagent 2>/dev/null && ! systemctl cat edgeshell-heartbeat 2>/dev/null').returncode == 0,
          'the classic install is gone: folder, service, user hbagent')
    ok, msg = heard()
    stats['first_heartbeat_after_move_s'] = round(time.time() - t0, 1)
    check(ok, 'heartbeats reach the broker from the new agent', msg)
    v = dx(node, '/opt/edgeshell/apps/heartbeat/current/venv/bin/python -c "import paho.mqtt, cryptography, yaml; print(\'libs ok\')"')
    check('libs ok' in v.stdout, "the agent's libraries are in its own venv", v.stdout)
    perms = dx(node, 'stat -c "%n %U:%G %a" /opt/edgeshell/apps/heartbeat/data/agent.env /opt/edgeshell/apps/heartbeat/data/mqtt.env').stdout.split('\n')
    check(all(p.endswith('root:es-heartbeat 640') for p in perms if p), "the node's keys and login: root:es-heartbeat 0640, inside a 0700 folder", perms)
    check(dx(node, 'su -s /bin/sh nobody -c "cat /opt/edgeshell/apps/heartbeat/data/agent.env" 2>/dev/null').returncode != 0,
          'other users cannot read the keys')
    left = dx(node, 'ls /root/Downloads/edgeshell; ls -d /tmp/edgeshell-hb.* 2>/dev/null; grep -rl e2e-pass-1 /root /tmp 2>/dev/null').stdout.strip()
    check(left == '', 'no package, token or password left in ~/Downloads/edgeshell or /tmp', left)
    inv = json.loads([l for l in dx(node, 'python3 /opt/edgeshell/bin/esapp inventory').stdout.splitlines() if l.startswith('ESAPP_RESULT ')][-1][13:])
    app = [x for x in inv['apps'] if x['name'] == 'heartbeat'][0]
    check(app['current'] == pkg.version and app['service'] == 'active', 'the Apps inventory shows the agent', app)

    # 4. the agent's own test, a settings push
    r = helper('test', {})
    check('HEARTBEAT_TEST ok' in r.stdout and 'service es-heartbeat running' in r.stdout, "the agent's own test passes", r.stdout[-800:])
    cfg = 'broker: host.docker.internal\nport: 1883\ntls: false\ninterval: 7\n'
    r = helper('config', {'config.yaml': cfg})
    time.sleep(12)
    j = dx(node, 'journalctl -u es-heartbeat --no-pager | grep -c "settings reloaded: interval=7"').stdout.strip()
    check('CONFIG_PUSHED' in r.stdout and j not in ('', '0'), 'a settings push is merged and hot reloaded', r.stdout[-400:])

    # 5. a bad update rolls itself back; a version that failed is not tried again
    bad2, bad2_sha = bad_package('1.3.9')
    r, s = install(bad2, '1.3.9', bad2_sha)
    stats['bad_update_rolled_back_s'] = round(s, 1)
    cur = dx(node, 'basename "$(readlink /opt/edgeshell/apps/heartbeat/current)"').stdout.strip()
    check(r.returncode != 0 and cur == pkg.version and dx(node, 'systemctl is-active --quiet es-heartbeat').returncode == 0,
          'a bad update rolls itself back and the agent keeps running', r.stdout[-800:])
    ok, msg = heard()
    check(ok, 'heartbeats continue after the rolled-back update', msg)
    r, s = install(bad2, '1.3.9', bad2_sha)
    check(r.returncode != 0 and 'failed here before' in r.stdout, 'the failed version is refused next time', r.stdout[-400:])

    # 6. the same version again (a repair): settings rewritten, service restarted
    r, s = install(app_service.package_path(pkg), pkg.version, pkg.sha256)
    check(r.returncode == 0 and 'HEARTBEAT_OK' in r.stdout, 'a repair with the same version restarts it cleanly', r.stdout[-600:])

    print('STATS heartbeat agent as an esapp app on Debian 12 systemd', json.dumps(stats, sort_keys=True))
    print('%d of %d checks passed' % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


if __name__ == '__main__':
    sys.exit(main())
