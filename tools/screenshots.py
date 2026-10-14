"""Regenerate the README screenshots from a local demo instance.

Starts EdgeShell in-process with a simulated fleet (made-up names and
addresses, nothing from a real deployment), turns on mandatory 2FA with the
built-in verifier, signs in through the real first-login flow, and captures
every main screen in Chromium. Three emulated Raspberry Pis (Pi Fleet
Emulator) give the terminals and Monitoring real data.

    for p in 2301 2302 2303; do docker run -d --rm --name shot-$p -p $p:22 devprincekumar/pi-fleet-node:1.0.0; done
    FLASK_ENV=testing python tools/screenshots.py --ssh 127.0.0.1:2301,127.0.0.1:2302,127.0.0.1:2303

Writes PNGs to docs/assets/screenshots/.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import struct
import sys
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('FLASK_ENV', 'testing')

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs', 'assets', 'screenshots')
SITES = ['north-plant', 'river-intake', 'east-works', 'lab-campus', 'south-depot', 'hill-reservoir']
FLEET = [('datalogger', 'logger', 22), ('floc-detector', 'floc', 7), ('bht', 'bht', 4), ('rtu', 'rtu', 2)]


def totp(secret, step=None):
    key = base64.b32decode(secret.replace(' ', '') + '=' * (-len(secret.replace(' ', '')) % 8), casefold=True)
    step = int(time.time() // 30) if step is None else step
    d = hmac.new(key, struct.pack('>Q', step), hashlib.sha1).digest()
    o = d[-1] & 15
    return str((struct.unpack('>I', d[o:o + 4])[0] & 0x7fffffff) % 1000000).zfill(6)


def fake_metrics(rng, arch):
    up = rng.randint(3, 60) * 86400 + rng.randint(0, 86000)
    total = rng.choice([1024, 2048, 4096, 8192]) * 1024
    used = int(total * rng.uniform(0.2, 0.75))
    disk_total = rng.choice([16, 32, 64])
    disk_pct = round(rng.uniform(12, 78), 1)
    load = round(rng.uniform(0.05, 1.6), 2)
    return {'cpu_percent': round(rng.uniform(2, 62), 1), 'mem_percent': round(used / total * 100, 1),
            'mem_total_mb': total // 1024, 'mem_used_mb': used // 1024, 'mem_free_mb': (total - used) // 1024,
            'disk_percent': disk_pct, 'disk_total_gb': disk_total,
            'disk_used_gb': round(disk_total * disk_pct / 100, 1),
            'load_1': load, 'load_5': round(load * 0.9, 2), 'load_15': round(load * 0.8, 2), 'load_trend': 'stable',
            'uptime': f'{up // 86400} days, {up % 86400 // 3600}:{up % 3600 // 60:02d}', 'uptime_seconds': up,
            'temperature': round(rng.uniform(41, 63), 1), 'arch': arch,
            'net_rx_bytes': rng.randint(10 ** 8, 10 ** 10), 'net_tx_bytes': rng.randint(10 ** 7, 10 ** 9),
            'docker_installed': rng.random() < 0.6, 'docker_running': rng.random() < 0.55,
            'nodered_installed': True, 'nodered_running': rng.random() < 0.85,
            'orgui_installed': False, 'orgui_running': False,
            'collected_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}


def seed(app, ssh):
    from app import db, _seed_defaults
    from app.models import ActivityLog, SystemMetrics
    from app.models.node import Node
    from app.models.user import User
    rng = random.Random(7)
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_defaults()
        a = User.query.filter_by(username='admin').first()
        a.must_change_password, a.email, a.max_sessions = False, 'owner@example.org', 4
        for name, role, email in (('maya.ops', 'operator', 'maya@example.org'), ('ravi.noc', 'monitor', 'ravi@example.org'),
                                  ('lena.admin', 'administrator', 'lena@example.org')):
            u = User(username=name, email=email, role=role, is_active=True, must_change_password=False, max_sessions=2)
            u.set_password('Demo-password-1')
            db.session.add(u)
        made, by_name = 0, {}
        for ntype, short, count in FLEET:
            for k in range(count):
                site = SITES[made % len(SITES)]
                r = rng.random()
                st = 'online' if r < 0.78 else ('offline' if r < 0.9 else 'auth_failed')
                arch = 'aarch64' if rng.random() < 0.7 else 'armv7l'
                n = Node(name=f'{site}-{short}-{k + 1:02d}', ip_address=f'100.{80 + made % 40}.{made + 10}.{k + 2}',
                         username='pi', node_type=ntype, environment='production' if made % 5 else 'staging',
                         location=site, tags=f'{ntype};tailscale', status=st, arch=arch)
                n.password = 'demo'
                if st == 'online':
                    m = fake_metrics(rng, arch)
                    n.cpu_percent, n.ram_percent, n.disk_percent = m['cpu_percent'], m['mem_percent'], m['disk_percent']
                    n.temperature, n.uptime = m['temperature'], m['uptime']
                    n.load_average = f"{m['load_1']} {m['load_5']} {m['load_15']}"
                    n.last_seen = datetime.utcnow()
                    by_name[n.name] = m
                elif st == 'auth_failed':
                    n.login_paused_at, n.login_pause_reason = datetime.utcnow(), 'password'
                    n.last_error = 'Authentication failed: check the SSH username and password. Automatic logins are paused.'
                else:
                    n.last_error = 'No answer from the node (timed out)'
                    n.last_seen = datetime.utcnow() - timedelta(hours=rng.randint(2, 50))
                db.session.add(n)
                made += 1
        for i, hp in enumerate(ssh):
            host, _, port = hp.partition(':')
            n = Node(name=f'lab-campus-pi-{i + 1:02d}', ip_address=host, ssh_port=int(port or 22), username='pi',
                     node_type='datalogger', environment='staging', location='lab-campus', status='online',
                     tags='datalogger;emulated')
            n.password = 'raspberry'
            db.session.add(n)
        for k in range(2):
            n = Node(name=f'old-site-logger-{k + 1:02d}', ip_address=f'100.99.0.{k + 2}', username='pi',
                     node_type='datalogger', is_deleted=True, is_active=False, deleted_at=datetime.utcnow())
            n.password = 'demo'
            db.session.add(n)
        db.session.commit()
        t0 = datetime.utcnow() - timedelta(days=3)
        rows = []
        for n in Node.query.filter(Node.is_deleted.isnot(True)).all():
            for k in range(0, 72 * 6):
                up = n.status == 'online' or k % 4
                rows.append(dict(node_id=n.id, timestamp=t0 + timedelta(minutes=10 * k), status='online' if up else 'offline',
                                 cpu_percent=rng.uniform(3, 55) if up else None, ram_percent=rng.uniform(25, 70) if up else None,
                                 disk_percent=rng.uniform(15, 60) if up else None, temperature=rng.uniform(42, 60) if up else None,
                                 uptime_seconds=3600 + 600 * k))
        db.session.bulk_insert_mappings(SystemMetrics, rows)
        for action, node, status in (('Bulk login update', None, 'success'), ('Logins paused', 'east-works-logger-03', 'warning'),
                                     ('Terminal session closed', 'lab-campus-pi-01', 'success'),
                                     ('Stats export started', None, 'success'), ('Bulk delete (recycle bin)', None, 'success')):
            db.session.add(ActivityLog(action=action, node=node, status=status, response='demo'))
        db.session.commit()
        demo = {n.id: by_name.get(n.name) for n in Node.query.all()}
    return demo


def fake_peers():
    rng = random.Random(3)
    peers = []
    for i in range(18):
        site = SITES[i % len(SITES)]
        online = i % 5 != 0
        peers.append({'name': f'{site}-node-{i:02d}', 'dns_name': f'{site}-node-{i:02d}.tail-demo.ts.net',
                      'ip': f'100.{90 + i}.{i * 7 % 250}.{i + 3}', 'os': ['linux', 'linux', 'windows', 'android'][i % 4],
                      'online': online, 'connection': 'direct' if i % 3 == 0 else ('relay blr' if online else ''),
                      'last_seen': (datetime.utcnow() - timedelta(hours=rng.randint(1, 300))).isoformat() + 'Z',
                      'rx_bytes': rng.randint(10 ** 6, 10 ** 10), 'tx_bytes': rng.randint(10 ** 6, 10 ** 9), 'exit_node': False})
    return {'connected': True, 'backend_state': 'Running', 'self_name': 'edgeshell', 'self_dns_name': 'edgeshell.tail-demo.ts.net',
            'self_ips': ['100.64.0.1', 'fd7a:115c:a1e0::1'], 'tailnet': 'tail-demo.ts.net', 'nodes': peers,
            'online_count': sum(p['online'] for p in peers), 'version': '1.98.8', 'health': []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ssh', default='')
    ap.add_argument('--port', type=int, default=5081)
    args = ap.parse_args()
    ssh = [s for s in args.ssh.split(',') if s]
    os.makedirs(OUT, exist_ok=True)

    from app import create_app
    app = create_app('testing')
    app.config.update(MFA_REQUIRED=True, MFA_PROVIDER='local', IDLE_MINUTES=0)
    demo = seed(app, ssh)
    import app.services.collector as col
    with app.app_context():
        from app.models.node import Node
        real = [n.id for n in Node.query.filter(Node.location == 'lab-campus', Node.tags.like('%emulated%'))]
        if real:
            col.COLLECTOR.run_cycle(node_ids=real, force=True)       # real metrics from the emulated Pis
    for nid, m in demo.items():
        if m:
            col.COLLECTOR.latest[nid] = {'ok': True, 'metrics': m, 'kind': None, 'message': None, 'seconds': 0.4,
                                         'at': datetime.utcnow()}
    threading.Thread(target=lambda: app.run(port=args.port, threaded=True, use_reloader=False), daemon=True).start()
    time.sleep(1.5)
    base = f'http://127.0.0.1:{args.port}'

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, device_scale_factor=2)
        page = ctx.new_page()

        def shot(name, full=False, clip=None, wait=900):
            page.wait_for_timeout(wait)
            path = os.path.join(OUT, name)
            page.screenshot(path=path, full_page=full, clip=clip)
            try:                                   # 256-colour PNG: about half the size, same look
                from PIL import Image
                Image.open(path).convert('RGB').quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(path, optimize=True)
            except ImportError:
                pass
            print('  saved', name)

        # first sign-in: password, then 2FA setup (QR), then backup codes
        page.goto(base + '/login')
        page.fill('#username', 'admin')
        page.fill('#password', 'admin123')
        page.click('#submitBtn')
        page.wait_for_selector('#secret')
        shot('2fa-setup.png')
        secret = page.inner_text('#secret')
        page.fill('input[name=code]', totp(secret))
        page.click('button[type=submit]')
        page.wait_for_selector('#backupCodes')
        shot('2fa-backup-codes.png')
        page.check('input[name=saved]')
        page.click('button[type=submit]')
        page.wait_for_url(lambda u: '/login' not in u)
        # a later sign-in: the 6-digit code step
        page.goto(base + '/logout')
        page.goto(base + '/login')
        page.fill('#username', 'admin')
        page.fill('#password', 'admin123')
        page.click('#submitBtn')
        page.wait_for_selector('#code')
        shot('2fa-verify.png', wait=300)
        page.fill('#code', totp(secret, int(time.time() // 30) + 1))
        page.click('button[type=submit]')
        page.wait_for_url(lambda u: '/login' not in u, timeout=15000)

        page.goto(base + '/')
        try:
            page.wait_for_selector('.cat-card', timeout=8000)
        except Exception:
            page.screenshot(path=os.path.join(OUT, '_debug.png'))
            print('  stuck at', page.url, page.inner_text('body')[:300].replace(chr(10), ' | '))
            raise
        shot('dashboard.png', wait=2500)

        page.goto(base + '/nodes/')
        page.wait_for_selector('.node-card')
        page.select_option('#catFilter', 'Data Loggers')
        shot('nodes.png')
        page.check('#selectAll')
        page.click('#bulkLoginBtn')
        page.wait_for_selector('#blUser')
        page.fill('#blUser', 'pi')
        shot('nodes-bulk-login.png')
        page.click('#esDlgCancel')
        page.click('#bulkClearBtn')
        page.select_option('#catFilter', '')
        page.click('button[data-bs-target="#recycleBin"]')
        shot('recycle-bin.png')
        page.keyboard.press('Escape')

        if real:
            page.goto(base + '/nodes/')
            page.fill('#nodeSearch', 'lab-campus-pi-01')
            page.evaluate('filterNodes()')
            page.locator(f'.es-shell-btn[data-id="{real[0]}"]').click()
            page.wait_for_function("document.querySelector('.es-shell .es-state').textContent === 'connected'", timeout=20000)
            page.wait_for_timeout(1500)
            page.locator('.es-shell .xterm-helper-textarea').focus()
            page.keyboard.type('uname -a; uptime; df -h / | tail -1; free -m | head -2\n')
            shot('node-terminal.png', wait=2500)
            page.click('.es-shell .es-close')

            page.goto(base + '/terminal/')
            page.wait_for_selector('#nodeList')
            page.evaluate("""ids => { document.querySelectorAll('.node-chk').forEach(c => {
                c.checked = ids.includes(+c.value) || c.closest('.node-item').dataset.status === 'offline' && +c.value % 3 === 0;
                c.dispatchEvent(new Event('change', {bubbles: true})); }); }""", real)
            page.fill('#cmdInput', '. /etc/os-release; echo "$PRETTY_NAME on $(uname -m), $(uptime -p)"')
            page.keyboard.press('Enter')
            page.wait_for_function("document.getElementById('termOut').textContent.includes(' up ')", timeout=30000)
            shot('terminal.png', wait=2500)

        page.goto(base + '/monitoring/')
        page.wait_for_selector('#autoRefreshToggle')
        shot('monitoring.png', wait=3000)

        page.route('**/tailscale/status', lambda route: route.fulfill(
            status=200, content_type='application/json', body=json.dumps(fake_peers())))
        page.goto(base + '/tailscale/')
        page.wait_for_selector('#peers-body table')
        page.select_option('#peer-status', 'online')
        shot('vpn.png', wait=800)
        page.unroute('**/tailscale/status')

        # phone: dashboard with the menu open
        phone = browser.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=3, is_mobile=True,
                                    has_touch=True, storage_state=ctx.storage_state())
        ppage = phone.new_page()
        page, desk = ppage, page
        page.goto(base + '/')
        page.wait_for_selector('.cat-card')
        shot('phone-dashboard.png', wait=2000)
        page.click('#sidebarToggle')
        shot('phone-menu.png', wait=600)
        page = desk

        page.goto(base + '/')
        page.wait_for_selector('.cat-card')
        page.click('#exportBtn')
        page.wait_for_selector('#expStart')
        page.select_option('#expPeriod', 'week')
        with page.expect_download(timeout=60000):
            page.click('#expStart')
        shot('stats-export.png', wait=600)
        page.click('#expClose')
        page.evaluate("""() => fetch('/nodes/jobs').then(r => r.json()).then(d =>
            sessionStorage.setItem('esJobsSeen', JSON.stringify(d.jobs.map(j => j.id))))""")   # hide its job card

        page.goto(base + '/account/')
        shot('account.png')
        page.goto(base + '/users/')
        shot('users.png')

        browser.close()


if __name__ == '__main__':
    main()
