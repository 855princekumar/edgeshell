"""EdgeShell end-to-end test against a Pi Fleet Emulator fleet.

Drives EdgeShell exactly as a browser would (session cookie + CSRF token)
and checks every fleet operation on every emulated node, plus the login,
two-factor (privacyIDEA) and session protections. It enrolls an authenticator
like a phone would, computing TOTP codes itself. Standard library only, so it
runs anywhere.

    EDGESHELL_URL=http://127.0.0.1:5050 PFE_URL=http://127.0.0.1:7070 python e2e.py

Exit code 0 only when every check passes. Run it through sandbox/run.sh,
which starts the stack, spawns the fleet and tears everything down.
"""
import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import secrets
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get('EDGESHELL_URL', 'http://127.0.0.1:5050').rstrip('/')
PFE = os.environ.get('PFE_URL', 'http://127.0.0.1:7070').rstrip('/')
NODE_PASSWORD = os.environ.get('PFE_NODE_PASSWORD', 'raspberry')
ADMIN_PASSWORD_FILE = os.environ.get('ADMIN_PASSWORD_FILE', '')
EXPECTED_NODES = int(os.environ.get('EXPECTED_NODES', '6'))
# Set to 127.0.0.1 when EdgeShell runs on the host instead of in the sandbox
# network, so host.docker.internal addresses in the export are rewritten.
HOST_OVERRIDE = os.environ.get('E2E_HOST_OVERRIDE', '')
FLEET_FILE = os.environ.get('FLEET_FILE',
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fleet.json'))

FAILED = []
_USED_STEPS = {}   # secret -> last TOTP time step sent (privacyIDEA rejects reuse)


def totp(secret, step):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack('>Q', step), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return str((struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)


def fresh_totp(secret):
    """A code for a time step not used yet, waiting for the next one if needed."""
    while True:
        now = int(time.time() // 30)
        step = max(now, _USED_STEPS.get(secret, -1) + 1)
        if step <= now + 1:            # verifiers accept one step of drift
            _USED_STEPS[secret] = step
            return totp(secret, step)
        time.sleep(2)


def check(name, ok, detail=''):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f'  ({detail})' if detail and not ok else ''))
    if not ok:
        FAILED.append(name)
    return ok


def section(title):
    print(f'\n== {title}')


class Client:
    """A browser-like session: cookie jar, CSRF token, no auto-redirects."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    def __init__(self, agent='EdgeShell-E2E'):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), self._NoRedirect())
        self.agent = agent
        self.csrf = None

    def request(self, method, path, data=None, headers=None, json_body=None):
        url = path if path.startswith('http') else BASE + path
        hdrs = {'User-Agent': self.agent}
        hdrs.update(headers or {})
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs['Content-Type'] = 'application/json'
            hdrs['Accept'] = 'application/json'
        if method != 'GET' and self.csrf:
            hdrs['X-CSRFToken'] = self.csrf
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with self.opener.open(req, timeout=180) as r:
                return r.status, r.headers, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode('utf-8', 'replace')

    def get(self, path, **kw):
        status, headers, body = self.request('GET', path, **kw)
        m = re.search(r'name="csrf-token" content="([^"]+)"', body) or \
            re.search(r'name="csrf_token" value="([^"]+)"', body)
        if m:
            self.csrf = m.group(1)
        return status, headers, body

    def post_form(self, path, fields):
        fields = dict(fields)
        fields.setdefault('csrf_token', self.csrf or '')
        data = urllib.parse.urlencode(fields, doseq=True).encode()
        return self.request('POST', path, data=data,
                            headers={'Content-Type': 'application/x-www-form-urlencoded'})

    def post_json(self, path, payload):
        status, _, body = self.request('POST', path, json_body=payload)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, {'_raw': body[:300]}

    def post_multipart(self, path, fields, files):
        boundary = uuid.uuid4().hex
        parts = []
        fields = list(fields) + [('csrf_token', self.csrf or '')]
        for k, v in fields:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fname, content) in files:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                         f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                         + content + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        return self.request('POST', path, data=b''.join(parts),
                            headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                                     'Accept': 'application/json'})

    def login(self, username, password, secret=None, code=None):
        """Password, then the second factor. With no secret on file, set up an
        authenticator (stored on self.secret / self.backup_codes). Returns the
        final (status, location, body); a takeover prompt comes back as 200."""
        self.get('/login')
        status, headers, body = self.post_form('/login', {'username': username, 'password': password})
        location = headers.get('Location', '')
        if status == 302 and '/login/setup' in location:
            _, _, page = self.get('/login/setup')
            m = re.search(r'id="secret">([A-Z2-7 ]+)<', page)
            if not m:
                return 500, '', page
            self.secret = m.group(1).replace(' ', '')
            status, headers, body = self.post_form('/login/setup', {'code': fresh_totp(self.secret)})
            self.backup_codes = re.findall(r'<span>([a-z2-9]{5}-[a-z2-9]{5})</span>', body)
            self.csrf = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
            status, headers, body = self.post_form('/login/setup/done', {'saved': '1'})
        elif status == 302 and '/login/verify' in location:
            self.get('/login/verify')
            status, headers, body = self.post_form(
                '/login/verify', {'code': code or fresh_totp(secret or self.secret)})
        if status == 200:
            m = re.search(r'name="csrf_token" value="([^"]+)"', body)
            if m:
                self.csrf = m.group(1)
        return status, headers.get('Location', ''), body

    secret = None
    backup_codes = ()

    def cookies(self):
        return list(self.jar)


def wait_for(url, seconds=180):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def expected_for(node):
    """Container name pi-emu-<debian><bits> -> (debian, uname -m)."""
    m = re.search(r'pi-emu-([a-z]+?)(32|64)', node.get('description') or '')
    if not m:
        return None, None
    return m.group(1), ('aarch64' if m.group(2) == '64' else 'armv7l')


def main():
    section('Stack')
    check('EdgeShell /healthz', wait_for(BASE + '/healthz'))
    check('Pi Fleet Emulator spawner', wait_for(PFE + '/api/health'))

    # ── Admin bootstrap ────────────────────────────────────────────────────
    section('Admin sign-in with mandatory 2FA (privacyIDEA)')
    admin = Client('Chrome/128.0 (Windows NT 10.0) E2E-admin')
    status, location, _ = admin.login('admin', 'admin123')
    check('first sign-in sets up an authenticator (QR + key)', bool(admin.secret))
    check('10 one-time backup codes issued', len(admin.backup_codes) == 10, len(admin.backup_codes))
    check('default admin must change password', status == 302 and '/change-password' in location,
          f'{status} {location}')
    admin_pw = 'E2e-' + secrets.token_urlsafe(12)
    admin.get('/change-password')
    status, _, _ = admin.post_form('/change-password', {
        'current_password': 'admin123', 'new_password': admin_pw, 'confirm_password': admin_pw})
    check('password changed', status == 302)
    status, headers, _ = admin.request('GET', '/nodes/')
    check('admin must add a personal email for reset links',
          status == 302 and '/account/email' in headers.get('Location', ''), status)
    admin.get('/account/email')
    admin.post_form('/account/email', {'email': 'owner@example.org', 'password': admin_pw})
    if ADMIN_PASSWORD_FILE:
        with open(ADMIN_PASSWORD_FILE, 'w') as f:
            json.dump({'password': admin_pw, 'backup': admin.backup_codes[-1]}, f)
    status, _, _ = admin.get('/')
    check('dashboard loads', status == 200, status)

    # ── Import the emulated fleet ──────────────────────────────────────────
    section('Import fleet from Pi Fleet Emulator export')
    with urllib.request.urlopen(PFE + '/api/export-edgeshell', timeout=30) as r:
        export = json.loads(r.read())
    # The emulator exports every running pi-emu-* container on the host, which
    # can include fleets from other projects. Keep only the nodes we spawned.
    with open(FLEET_FILE) as f:
        ours = {f"(pi-emu-{n['name']})" for n in json.load(f)['nodes']}
    export['nodes'] = [n for n in export.get('nodes', [])
                       if any(tag in (n.get('description') or '') for tag in ours)]
    export['node_count'] = len(export['nodes'])
    check(f'export lists the {EXPECTED_NODES} sandbox nodes', export['node_count'] == EXPECTED_NODES,
          export['node_count'])
    admin.get('/nodes/import')
    status, _, body = admin.post_multipart('/nodes/import', [],
                                           [('file', ('pi-fleet-nodes.json', json.dumps(export).encode()))])
    check('JSON export parsed for verification', status == 200 and 'map__ip_address' in body, status)
    admin.csrf = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
    mapping = {f'map__{h}': h for h in ('name', 'ip_address', 'ssh_port', 'username',
                                         'description', 'tags')}
    status, _, _ = admin.post_form('/nodes/import/commit',
                                   dict(mapping, default_password=NODE_PASSWORD,
                                        host_override=HOST_OVERRIDE))
    check('import committed', status == 302, status)
    admin.get('/nodes/')

    status, _, body = admin.request('GET', '/api/v1/nodes')
    nodes = json.loads(body) if status == 200 else []
    check(f'{EXPECTED_NODES} nodes registered', len(nodes) == EXPECTED_NODES, len(nodes))
    offline = [n['name'] for n in nodes if n['status'] != 'online']
    check('every node reachable over SSH after import', not offline, offline)
    ids = [n['id'] for n in nodes]
    by_name = {n['name']: n for n in nodes}

    # ── Per-node connection test + architecture detection ─────────────────
    section('Connection test and architecture detection')
    for n in nodes:
        _, want_arch = expected_for(n)
        status, r = admin.post_json(f"/nodes/{n['id']}/test", {})
        check(f"{n['name']}: SSH ok, arch {want_arch}",
              r.get('success') and r.get('arch') == want_arch, r)

    # ── Remote command on every node (OS and arch spread) ─────────────────
    section('Fleet-wide command')
    status, r = admin.post_json('/terminal/execute', {
        'node_ids': ids, 'command': '. /etc/os-release; echo "$VERSION_CODENAME $(uname -m)"'})
    results = r.get('results', {})
    for name, n in by_name.items():
        want_debian, want_arch = expected_for(n)
        out = (results.get(name) or {}).get('stdout', '')
        check(f'{name}: reports {want_debian} {want_arch}',
              want_debian in out and want_arch in out, out.strip()[-80:])
    codenames = {expected_for(n)[0] for n in nodes}
    check('fleet spans multiple Debian releases', len(codenames) >= 3, codenames)

    # ── Monitoring ─────────────────────────────────────────────────────────
    section('Monitoring')
    status, r = admin.post_json('/monitoring/poll', {'node_ids': ids})
    res = r.get('results', {})
    good = [v for v in res.values() if v.get('success')
            and isinstance((v.get('metrics') or {}).get('cpu_percent'), (int, float))
            and isinstance((v.get('metrics') or {}).get('mem_percent'), (int, float))]
    check('metrics collected from every node', len(good) == EXPECTED_NODES, f'{len(good)}/{len(res)}')
    status, r = admin.post_json('/nodes/capabilities/scan', {})
    rows = r.get('nodes', [])
    check('Node-RED detected on every node', rows and all(x.get('nodered') for x in rows),
          [(x['name'], x.get('nodered')) for x in rows])

    # ── Push / pull data ───────────────────────────────────────────────────
    section('Data push and pull')
    token = secrets.token_hex(8)
    fname = f'e2e-{token}.txt'
    status, _, body = admin.post_multipart(
        '/files/upload', [('node_ids', str(i)) for i in ids] + [('target_path', '/tmp/')],
        [('files', (fname, f'edgeshell-e2e {token}\n'.encode()))])
    up = json.loads(body) if status == 200 else {}
    check('file pushed to every node', status == 200 and up.get('success'), body[:200])
    pulled = 0
    for n in nodes:
        _, r = admin.post_json('/logs/fetch', {'node_id': n['id'], 'file_path': f'/tmp/{fname}'})
        pulled += token in (r.get('content') or '')
    check('file pulled back from every node', pulled == EXPECTED_NODES, f'{pulled}/{EXPECTED_NODES}')

    # ── OTA-style rollout ──────────────────────────────────────────────────
    section('OTA-style rollout (push update, apply with sudo, verify)')
    version = f'2.0.{int(time.time())}'
    script = (f'#!/bin/sh\nset -e\nmkdir -p /opt/edgeshell-ota\n'
              f'echo "{version}" > /opt/edgeshell-ota/VERSION\n').encode()
    status, _, body = admin.post_multipart(
        '/files/upload', [('node_ids', str(i)) for i in ids] + [('target_path', '/tmp/')],
        [('files', ('edgeshell-ota.sh', script))])
    check('update package pushed', status == 200 and json.loads(body).get('success'), body[:200])
    status, r = admin.post_json('/terminal/execute', {
        'node_ids': ids, 'command': 'sudo sh /tmp/edgeshell-ota.sh && cat /opt/edgeshell-ota/VERSION'})
    applied = [k for k, v in r.get('results', {}).items() if version in (v.get('stdout') or '')]
    check('update applied with sudo on every node', len(applied) == EXPECTED_NODES,
          f'{len(applied)}/{EXPECTED_NODES}')

    # ── Login and session security ─────────────────────────────────────────
    section('Two-factor checks')
    probe = Client('Firefox/130.0 (Linux) E2E-2fa-probe')
    probe.get('/login')
    probe.post_form('/login', {'username': 'admin', 'password': admin_pw})
    probe.get('/login/verify')
    status, _, body = probe.post_form('/login/verify', {'code': '000000'})
    check('wrong 2FA code rejected', status == 200 and 'not valid' in body, status)
    check('nothing signed in before the second factor', probe.request('GET', '/nodes/')[0] == 302)
    probe.get('/login/cancel')
    status, _, body = probe.login('admin', admin_pw, code=admin.backup_codes[0])
    check('backup code accepted in place of the app code',
          status == 200 and 'already signed in' in body, status)
    probe.get('/login/cancel')
    status, _, body = probe.login('admin', admin_pw, code=admin.backup_codes[0])
    check('the same backup code is refused the second time', 'not valid' in body, status)
    probe.get('/login/cancel')

    section('Session limits')
    other = Client('Firefox/130.0 (Linux) E2E-second-device')
    status, _, body = other.login('admin', admin_pw, secret=admin.secret)
    check('admin limited to 1 device: second sign-in must confirm',
          status == 200 and 'already signed in' in body, status)
    other.get('/login/cancel')
    check('cancel keeps the first session', admin.get('/')[0] == 200)

    op_pw = 'Op-' + secrets.token_urlsafe(10)
    admin.get('/users/add')
    status, _, _ = admin.post_form('/users/add', {
        'username': 'e2e-operator', 'email': 'e2e-operator@example.test', 'role': 'operator',
        'password': op_pw, 'confirm_password': op_pw, 'max_sessions': '2'})
    check('operator created with 2-device limit', status == 302, status)
    devices = []
    op_secret = None
    for i in range(2):
        c = Client(f'Mozilla/5.0 (iPhone) Mobile Safari/17 E2E-op-{i}' if i == 0
                   else 'Mozilla/5.0 (iPad) Safari/17 E2E-op-tablet')
        c.login('e2e-operator', op_pw, secret=op_secret)
        op_secret = op_secret or c.secret
        if i == 0:
            c.get('/change-password')
            new_pw = op_pw + 'x'
            c.post_form('/change-password', {'current_password': op_pw,
                                             'new_password': new_pw, 'confirm_password': new_pw})
            op_pw = new_pw
        devices.append(c)
    check('2 devices signed in at once', all(c.get('/')[0] == 200 for c in devices))
    account = devices[1].get('/account/')[2]
    check('My account lists both devices by type', 'Mobile #' in account and 'Tablet #' in account)
    third = Client('Edge/128 (Windows) E2E-op-2')
    status, _, body = third.login('e2e-operator', op_pw, secret=op_secret)
    check('3rd device must confirm signing out the oldest',
          status == 200 and 'already signed in' in body, status)
    third.post_form('/login/confirm', {})
    status, _ = devices[0].post_json('/monitoring/poll', {'node_ids': []})
    check('oldest device signed out after confirm', status == 401, status)

    section('Stolen cookie after logout')
    thief = Client('curl/8 E2E-thief')
    for ck in third.cookies():
        thief.jar.set_cookie(ck)
    third.get('/')
    third.request('GET', '/logout')
    status, _, _ = thief.request('GET', '/api/v1/nodes', headers={'Accept': 'application/json'})
    check('copied cookie rejected once the owner logs out', status in (302, 401), status)

    section('Password reset ends sessions and resets 2FA')
    users_html = admin.get('/users/')[2]
    m = re.search(r'/users/(\d+)/reset-password" class="d-inline es-reset" data-user="e2e-operator"', users_html)
    op_id = m.group(1) if m else None
    check('operator listed in User Management', op_id is not None)
    if op_id:
        temp_pw = 'Reset-' + secrets.token_hex(6)
        admin.post_form(f'/users/{op_id}/reset-password', {'new_password': temp_pw})
        status, _ = devices[1].post_json('/monitoring/poll', {'node_ids': []})
        check('operator signed out by admin password reset', status == 401, status)
        again = Client('E2E-op-after-reset')
        again.login('e2e-operator', temp_pw)
        check('2FA must be set up again after an admin reset', bool(again.secret))

    section('Brute force and username discovery')
    attacker = Client('python-requests E2E-attacker')
    bodies = [attacker.login('e2e-operator', f'wrong-{i}')[2] for i in range(5)]
    locked_body = attacker.login('e2e-operator', 'anything')[2]
    unknown_body = attacker.login('no-such-user-' + token, 'x')[2]
    generic = 'temporarily locked'
    check('same message for wrong password, locked account and unknown user',
          all(generic in b for b in bodies + [locked_body, unknown_body]))
    check('no "Account locked" disclosure', all('Account locked.' not in b for b in bodies + [locked_body]))

    section('Role-based access')
    mon_pw = 'Mon-' + secrets.token_urlsafe(10)
    admin.get('/users/add')
    admin.post_form('/users/add', {'username': 'e2e-monitor', 'email': 'e2e-monitor@example.test',
                                   'role': 'monitor', 'password': mon_pw, 'confirm_password': mon_pw,
                                   'max_sessions': '1'})
    mon = Client('E2E-monitor')
    mon.login('e2e-monitor', mon_pw)
    mon.get('/change-password')
    mon.post_form('/change-password', {'current_password': mon_pw, 'new_password': mon_pw + 'm',
                                       'confirm_password': mon_pw + 'm'})
    status, _ = mon.post_json('/terminal/execute', {'node_ids': ids, 'command': 'id'})
    check('monitor cannot run commands', status == 403, status)
    status, _, _ = mon.post_multipart('/files/upload', [('node_ids', str(ids[0]))],
                                      [('files', ('x.txt', b'x'))])
    check('monitor cannot deploy files', status in (302, 403), status)

    section('Sign-up approval and admin protection')
    newbie = Client('E2E-signup')
    newbie.get('/signup')
    newbie_pw = 'New-' + secrets.token_urlsafe(10)
    newbie.post_form('/signup', {'username': 'e2e-newbie', 'email': 'newbie@example.org',
                                 'password': newbie_pw, 'confirm_password': newbie_pw})
    status, _, body = newbie.login('e2e-newbie', newbie_pw)
    check('sign-up waits for approval', 'waiting for an administrator' in body, status)
    users_html = admin.get('/users/')[2]
    m = re.search(r'/users/(\d+)/approve', users_html)
    check('sign-up listed for approval', m is not None)
    if m:
        admin.post_form(f'/users/{m.group(1)}/approve', {'role': 'operator', 'max_sessions': '1'})
        status, _, _ = newbie.login('e2e-newbie', newbie_pw)
        check('approved user signs in after setting up 2FA', newbie.secret and newbie.get('/')[0] == 200)

    adm2_pw = 'Adm-' + secrets.token_urlsafe(10)
    admin.get('/users/add')
    admin.post_form('/users/add', {'username': 'e2e-admin2', 'email': 'admin2@example.org',
                                   'role': 'administrator', 'password': adm2_pw,
                                   'confirm_password': adm2_pw, 'max_sessions': '1'})
    adm2 = Client('E2E-admin2')
    adm2.login('e2e-admin2', adm2_pw)
    adm2.get('/change-password')
    adm2.post_form('/change-password', {'current_password': adm2_pw, 'new_password': adm2_pw + 'z',
                                        'confirm_password': adm2_pw + 'z'})
    adm2.get('/users/')
    adm2.post_form('/users/1/role', {'role': 'monitor'})
    refused = 'nobody can change the owner' in adm2.get('/users/')[2]
    check('a second admin cannot demote the owner', refused and admin.get('/users/')[0] == 200)

    print()
    if FAILED:
        print(f'FAILED: {len(FAILED)} check(s)')
        for f in FAILED:
            print(f'  - {f}')
        return 1
    print('ALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
