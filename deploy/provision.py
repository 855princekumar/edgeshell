"""EdgeShell production provisioner (Debian / Ubuntu EC2).

Invoked by `python3 run.py` on Linux. Standard library only, because it runs
before the virtualenv exists. Idempotent: the same command installs the first
time and updates every time after.

  Layout          /opt/edgeshell            code (root-owned, read-only to the app)
                  /opt/edgeshell/.env       config + secrets (root:edgeshell 0640)
                  /var/lib/edgeshell        service state (HOME, restart marker)
                  /etc/edgeshell/secrets.env  DB + phpMyAdmin credentials (root 0600)
  Network         Gunicorn 127.0.0.1:PORT  <-  Nginx :80/:443 (domain only)
                  phpMyAdmin  <tailscale-ip>:PMA_PORT  (tailnet only)
  Database        MariaDB (stock config), unix socket, DB `edgeshell`
"""
import getpass
import json
import os
import platform
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.environ.get('EDGESHELL_APP_DIR', '/opt/edgeshell')
STATE_DIR = '/var/lib/edgeshell'
ETC_DIR = '/etc/edgeshell'
SECRETS = os.path.join(ETC_DIR, 'secrets.env')
CRED_REPORT = '/root/edgeshell-credentials.txt'
SVC_USER = 'edgeshell'
DB_NAME = 'edgeshell'
DB_APP_USER = 'edgeshell_app'
DB_ADMIN_USER = 'edgeshell_dba'
SOCK = '/run/mysqld/mysqld.sock'
TPL = os.path.join(SRC, 'deploy', 'templates')
ACME_ROOT = '/var/www/letsencrypt'
NO_SYSTEMD = os.environ.get('EDGESHELL_NO_SYSTEMD') == '1'   # sandbox testing only
# Host firewall. 'ufw' (default) allows only ssh, 80, 443, Tailscale and the
# tailnet interface; 'off' leaves the host firewall alone (security group only).
FIREWALL = os.environ.get('EDGESHELL_FIREWALL', 'ufw').strip().lower()
TAILSCALE_UDP = 41641
# Public listeners that are expected: web, Tailscale direct, DHCP clients.
ALLOWED_PUBLIC = {('tcp', 80), ('tcp', 443), ('udp', TAILSCALE_UDP), ('udp', 68), ('udp', 546)}

APT_BASE = ['python3-venv', 'python3-pip', 'rsync', 'curl', 'git', 'openssh-client',
            'sshpass', 'mariadb-server', 'nginx', 'php-fpm', 'php-mysql',
            'php-mbstring', 'php-zip', 'php-gd', 'php-curl', 'php-xml', 'php-bz2',
            'phpmyadmin', 'ca-certificates', 'ufw', 'iproute2']
APT_TLS = ['certbot']

MANAGED_ENV_ORDER = [
    'FLASK_ENV', 'FLASK_DEBUG', 'SECRET_KEY', 'CREDENTIAL_KEY', 'DATABASE_URL',
    'HOST', 'PORT', 'GUNICORN_THREADS', 'BEHIND_PROXY', 'SESSION_COOKIE_SECURE',
    'SESSION_HOURS', 'SINGLE_SESSION', 'MAX_FAILED_LOGINS', 'LOCKOUT_MINUTES',
    'DISPLAY_TZ', 'EDGESHELL_DOMAIN', 'LETSENCRYPT_EMAIL', 'PMA_PORT',
    'VPN_BACKEND', 'WIREGUARD_INTERFACE', 'TAILSCALE_AUTH_KEY',
]
PLACEHOLDERS = ('CHANGE_ME', 'your-key-here', 'your_api_key_here')


# ── output ───────────────────────────────────────────────────────────────────
C = {'b': '\033[94m', 'g': '\033[92m', 'y': '\033[93m', 'r': '\033[91m', 'x': '\033[0m', 'B': '\033[1m'}
STEPS = []


def step(msg):
    print(f"\n  {C['b']}▶{C['x']}  {C['B']}{msg}{C['x']}")


def ok(msg):
    print(f"  {C['g']}✔{C['x']}  {msg}")


def warn(msg):
    print(f"  {C['y']}⚠{C['x']}  {msg}")
    STEPS.append(('warn', msg))


def die(msg):
    print(f"  {C['r']}✘{C['x']}  {msg}")
    sys.exit(1)


def sh(cmd, check=True, capture=False, env=None, input_text=None, quiet=False):
    """Run a command list. Returns CompletedProcess."""
    r = subprocess.run(cmd, check=False, text=True, env=env, input=input_text,
                       capture_output=capture or quiet)
    if check and r.returncode != 0:
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        die(f"Command failed ({r.returncode}): {' '.join(cmd)}\n{out[-2000:]}")
    return r


def systemctl(*args, check=True):
    if NO_SYSTEMD:
        return None
    return sh(['systemctl', *args], check=check, quiet=True)


def write(path, content, mode=0o644, owner=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', newline='\n') as f:
        f.write(content)
    os.chmod(tmp, mode)
    if owner:
        shutil.chown(tmp, *owner)
    os.replace(tmp, path)


def ipv6_ok():
    try:
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s6.close()
        return True
    except OSError:
        return False


def tpl(name, **values):
    with open(os.path.join(TPL, name)) as f:
        s = f.read()
    for k, v in values.items():
        s = s.replace(f'@@{k}@@', str(v))
    if name.startswith('nginx-') and not ipv6_ok():
        s = '\n'.join(l for l in s.splitlines() if not l.strip().startswith('listen [::]')) + '\n'
    left = re.findall(r'@@[A-Z_]+@@', s)
    if left:
        die(f'Template {name} has unfilled placeholders: {left}')
    return s


# ── env files ────────────────────────────────────────────────────────────────
def parse_env(path):
    """Minimal dotenv reader: KEY=VALUE, strips CR, inline comments, quotes."""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip().replace('\r', '')
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            k, v = k.strip(), v.strip()
            if v[:1] in ('"', "'") and v[-1:] == v[:1]:
                v = v[1:-1]
            else:
                v = re.split(r'\s+#', v, 1)[0].strip()
            out[k] = v
    return out


def render_env(values):
    """Write .env cleanly: LF endings, no inline comments."""
    lines = ['# EdgeShell production environment. Generated by run.py.',
             '# Edit values here or in the source .env, then re-run: sudo python3 run.py',
             '# Timestamps are stored in UTC; DISPLAY_TZ only affects display.', '']
    seen = set()
    for k in MANAGED_ENV_ORDER:
        if k in values:
            lines.append(f'{k}={values[k]}')
            seen.add(k)
    extra = sorted(k for k in values if k not in seen)
    if extra:
        lines += ['', '# Other settings']
        lines += [f'{k}={values[k]}' for k in extra]
    return '\n'.join(lines) + '\n'


def usable(v):
    return bool(v) and not any(p in v for p in PLACEHOLDERS)


# ── steps ────────────────────────────────────────────────────────────────────
def preflight():
    step('Preflight')
    if platform.system() != 'Linux':
        die('Production install is for Linux. On Windows use: python run.py --dev')
    if os.geteuid() != 0:
        die('Run as root: sudo python3 run.py')
    if not shutil.which('apt-get'):
        die('Only Debian/Ubuntu (apt) are supported. Recommended: Ubuntu 24.04 or 26.04 LTS on EC2.')
    if not NO_SYSTEMD and not os.path.isdir('/run/systemd/system'):
        die('systemd is not running on this host.')
    if sys.version_info < (3, 10):
        die(f'Python 3.10+ required, found {platform.python_version()}.')
    osr = parse_env('/etc/os-release')
    ok(f"{osr.get('PRETTY_NAME', 'Linux')}, Python {platform.python_version()}, source {SRC}")


def load_settings(args):
    step('Configuration')
    src_env = parse_env(os.path.join(SRC, '.env'))
    tgt_env = parse_env(os.path.join(APP_DIR, '.env'))
    example = parse_env(os.path.join(SRC, '.env.example'))

    def pick(key, default=''):
        for d in (src_env, tgt_env):
            if usable(d.get(key)):
                return d[key]
        return default

    cfg = {
        'domain': pick('EDGESHELL_DOMAIN').lower().strip().rstrip('.'),
        'email': pick('LETSENCRYPT_EMAIL'),
        'tz': pick('DISPLAY_TZ', 'Asia/Kolkata'),
        'port': pick('PORT', '5000'),
        'pma_port': pick('PMA_PORT', '8080'),
        'set_system_tz': pick('SET_SYSTEM_TZ', 'true').lower() in ('1', 'true', 'yes'),
    }
    interactive = sys.stdin.isatty() and not args.get('yes')
    if not cfg['domain'] and interactive:
        cfg['domain'] = input('  Subdomain for EdgeShell (e.g. fleet.example.com, blank = none): ').strip().lower()
    if cfg['domain'] and not cfg['email'] and interactive:
        cfg['email'] = input("  Email for Let's Encrypt TLS (blank = HTTP only): ").strip()
    if cfg['domain'] and not re.fullmatch(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+', cfg['domain']):
        die(f"Invalid domain: {cfg['domain']}")
    cfg['src_env'], cfg['tgt_env'], cfg['example_env'] = src_env, tgt_env, example
    ok(f"domain={cfg['domain'] or '(none)'}  tls_email={cfg['email'] or '(none)'}  "
       f"tz={cfg['tz']}  app=127.0.0.1:{cfg['port']}  pma_port={cfg['pma_port']}")
    return cfg


def apt_install(cfg):
    step('System packages')
    wanted = APT_BASE + (APT_TLS if cfg['domain'] and cfg['email'] else [])
    missing = [p for p in wanted
               if sh(['dpkg-query', '-W', '-f=${Status}', p], check=False, capture=True)
               .stdout.find('install ok installed') < 0]
    if not missing:
        ok('All packages present.')
        return
    env = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
    # phpMyAdmin: no Apache, no dbconfig (we configure it ourselves).
    sh(['debconf-set-selections'], input_text=(
        'phpmyadmin phpmyadmin/dbconfig-install boolean false\n'
        'phpmyadmin phpmyadmin/reconfigure-webserver multiselect\n'), quiet=True)
    print(f"     installing: {' '.join(missing)}")
    r = sh(['apt-get', 'update', '-qq'], env=env, check=False, capture=True)
    if r.returncode != 0:  # e.g. a stale third-party repo; the Ubuntu mirrors may still be fine
        warn('apt-get update reported errors (continuing): ' + (r.stderr or '').strip().splitlines()[-1][:200])
    sh(['apt-get', 'install', '-y', '-qq', '--no-install-recommends', *missing], env=env, quiet=True)
    if shutil.which('apache2'):
        warn('apache2 is installed on this host; it may hold port 80. Stop it: systemctl disable --now apache2')
    ok(f'Installed {len(missing)} package(s).')


def set_timezone(cfg):
    step('Timezone')
    if not cfg['set_system_tz']:
        ok('SET_SYSTEM_TZ=false; leaving system timezone unchanged.')
        return
    if not os.path.isfile(f"/usr/share/zoneinfo/{cfg['tz']}"):
        warn(f"Unknown timezone {cfg['tz']}; system timezone unchanged.")
        return
    if not NO_SYSTEMD and shutil.which('timedatectl'):
        sh(['timedatectl', 'set-timezone', cfg['tz']], check=False, quiet=True)
    ok(f"System clock (journal, nginx logs) shows {cfg['tz']}. DB stays UTC; UI shows {cfg['tz']}.")


def ensure_user():
    step('Service user and directories')
    try:
        pwd.getpwnam(SVC_USER)
    except KeyError:
        sh(['useradd', '--system', '--home-dir', STATE_DIR, '--shell', '/usr/sbin/nologin',
            '--user-group', SVC_USER])
    for d, mode, own in ((STATE_DIR, 0o750, (SVC_USER, SVC_USER)),
                         (ETC_DIR, 0o700, ('root', 'root')),
                         (APP_DIR, 0o750, ('root', SVC_USER))):
        os.makedirs(d, exist_ok=True)
        os.chmod(d, mode)
        shutil.chown(d, *own)
    ok(f'user={SVC_USER}  app={APP_DIR}  state={STATE_DIR}')


def sync_code():
    step('Code')
    same = os.path.realpath(SRC) == os.path.realpath(APP_DIR)
    if same:
        ok('Running from the install directory; no copy needed.')
    else:
        excludes = ['venv/', '.venv/', '__pycache__/', '*.pyc', '.git/', '.env',
                    'instance/', 'uploads/', 'ansible/inventory/', '.pytest_cache/']
        cmd = ['rsync', '-a', '--delete']
        for e in excludes:
            cmd += ['--exclude', e]
        sh(cmd + [SRC.rstrip('/') + '/', APP_DIR + '/'])
        # Merge user uploads (never delete on the server).
        if os.path.isdir(os.path.join(SRC, 'uploads')):
            sh(['rsync', '-a', os.path.join(SRC, 'uploads') + '/', os.path.join(APP_DIR, 'uploads') + '/'])
        ok(f'Synced {SRC} -> {APP_DIR}')

    # Code: root-owned, readable by the service. Runtime dirs: writable by it.
    sh(['chown', '-R', f'root:{SVC_USER}', APP_DIR])
    sh(['chmod', '-R', 'u=rwX,g=rX,o=', APP_DIR])
    for d in ('instance', 'uploads', 'ansible/inventory'):
        p = os.path.join(APP_DIR, d)
        os.makedirs(p, exist_ok=True)
        sh(['chown', '-R', f'{SVC_USER}:{SVC_USER}', p])
        sh(['chmod', '-R', 'u=rwX,g=rX,o=', p])

    # Stage the old SQLite DB once for migration.
    marker = os.path.join(STATE_DIR, '.sqlite-migrated')
    src_db = os.path.join(SRC, 'instance', 'edgeshell.db')
    dst_db = os.path.join(APP_DIR, 'instance', 'edgeshell.db')
    if not same and os.path.isfile(src_db) and not os.path.exists(marker) and not os.path.exists(dst_db):
        for ext in ('', '-wal', '-shm'):
            if os.path.isfile(src_db + ext):
                shutil.copy2(src_db + ext, dst_db + ext)
                shutil.chown(dst_db + ext, SVC_USER, SVC_USER)
        ok('Staged existing SQLite database for migration to MariaDB.')


def load_secrets():
    s = parse_env(SECRETS)
    changed = False
    for k in ('DB_APP_PASS', 'DB_ADMIN_PASS', 'PMA_BASIC_PASS'):
        if not s.get(k):
            s[k] = secrets.token_hex(20)
            changed = True
    if not s.get('PMA_BASIC_USER'):
        s['PMA_BASIC_USER'] = 'pma-' + secrets.token_hex(3)
        changed = True
    if changed:
        write(SECRETS, ''.join(f'{k}={v}\n' for k, v in sorted(s.items())), 0o600, ('root', 'root'))
    return s


def db_url(sec):
    return (f"mysql+mysqlconnector://{DB_APP_USER}:{sec['DB_APP_PASS']}@localhost/{DB_NAME}"
            f"?unix_socket={SOCK}&charset=utf8mb4&collation=utf8mb4_unicode_ci")


def write_env(cfg, sec, cookie_secure=False):
    src, tgt, ex = cfg['src_env'], cfg['tgt_env'], cfg['example_env']
    vals = {}
    for d in (ex, tgt, src):          # source folder .env wins for general settings
        for k, v in d.items():
            if usable(v):
                vals[k] = v
    for key in ('SECRET_KEY', 'CREDENTIAL_KEY'):  # existing server key wins
        v = tgt.get(key) if usable(tgt.get(key)) else src.get(key)
        if not usable(v):
            if key == 'CREDENTIAL_KEY' and os.path.isfile(os.path.join(APP_DIR, 'instance', 'edgeshell.db')):
                warn('No CREDENTIAL_KEY found: migrated node SSH passwords will not decrypt. Re-enter them in the UI.')
            v = secrets.token_hex(32)
        vals[key] = v
    vals.update({
        'FLASK_ENV': 'production', 'FLASK_DEBUG': 'false',
        'DATABASE_URL': db_url(sec), 'HOST': '127.0.0.1', 'PORT': cfg['port'],
        'BEHIND_PROXY': 'true', 'SESSION_COOKIE_SECURE': 'true' if cookie_secure else 'false',
        'DISPLAY_TZ': cfg['tz'], 'EDGESHELL_DOMAIN': cfg['domain'],
        'LETSENCRYPT_EMAIL': cfg['email'], 'PMA_PORT': cfg['pma_port'],
    })
    vals.setdefault('SINGLE_SESSION', 'true')
    vals.setdefault('GUNICORN_THREADS', '16')
    vals.setdefault('VPN_BACKEND', 'tailscale')
    for k in [k for k, v in vals.items() if not usable(v) and k not in ('EDGESHELL_DOMAIN', 'LETSENCRYPT_EMAIL')]:
        vals.pop(k)
    vals.pop('SET_SYSTEM_TZ', None)
    write(os.path.join(APP_DIR, '.env'), render_env(vals), 0o640, ('root', SVC_USER))
    cfg['tgt_env'] = vals


def build_venv():
    step('Python environment')
    venv = os.path.join(APP_DIR, 'venv')
    py = os.path.join(venv, 'bin', 'python')
    if not os.path.isfile(py):
        sh([sys.executable, '-m', 'venv', venv])
    sh([py, '-m', 'pip', 'install', '-q', '--upgrade', 'pip'], quiet=True)
    sh([py, '-m', 'pip', 'install', '-q', '-r', os.path.join(APP_DIR, 'requirements.txt')], quiet=True)
    sh(['chown', '-R', f'root:{SVC_USER}', venv])
    sh(['chmod', '-R', 'u=rwX,g=rX,o=', venv])
    ver = sh([py, '-c', 'import flask,sqlalchemy,mysql.connector as m;'
              'print(flask.__version__, sqlalchemy.__version__, m.__version__)'], capture=True).stdout.split()
    ok(f'venv ready: Flask {ver[0]}, SQLAlchemy {ver[1]}, mysql-connector {ver[2]}')


def mysql(sql, db=None):
    cmd = ['mariadb', '--protocol=socket', '-uroot', '-N', '-B']
    if db:
        cmd.append(db)
    return sh(cmd, input_text=sql, capture=True).stdout.strip()


def setup_mariadb(sec):
    step('MariaDB')
    systemctl('enable', '--now', 'mariadb')
    for _ in range(30):
        if os.path.exists(SOCK):
            break
        time.sleep(1)
    else:
        die('MariaDB socket did not appear.')
    host = socket.gethostname().replace("'", '')
    mysql(f"""
        -- equivalent of mysql_secure_installation (non-interactive)
        DROP USER IF EXISTS ''@'localhost';
        DROP USER IF EXISTS ''@'{host}';
        DELETE FROM mysql.global_priv WHERE User='root' AND Host NOT IN ('localhost','127.0.0.1','::1');
        DROP DATABASE IF EXISTS test;
        DELETE FROM mysql.db WHERE Db='test' OR Db='test\\\\_%';
        CREATE DATABASE IF NOT EXISTS {DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
        CREATE USER IF NOT EXISTS '{DB_APP_USER}'@'localhost' IDENTIFIED BY '{sec['DB_APP_PASS']}';
        ALTER USER '{DB_APP_USER}'@'localhost' IDENTIFIED BY '{sec['DB_APP_PASS']}';
        GRANT ALL PRIVILEGES ON {DB_NAME}.* TO '{DB_APP_USER}'@'localhost';
        CREATE USER IF NOT EXISTS '{DB_ADMIN_USER}'@'localhost' IDENTIFIED BY '{sec['DB_ADMIN_PASS']}';
        ALTER USER '{DB_ADMIN_USER}'@'localhost' IDENTIFIED BY '{sec['DB_ADMIN_PASS']}';
        GRANT ALL PRIVILEGES ON {DB_NAME}.* TO '{DB_ADMIN_USER}'@'localhost';
        GRANT PROCESS ON *.* TO '{DB_ADMIN_USER}'@'localhost';
        FLUSH PRIVILEGES;
    """)
    bind = mysql('SELECT @@bind_address')
    ver = mysql('SELECT VERSION()')
    ok(f'MariaDB {ver} (bind {bind}), database `{DB_NAME}`, users {DB_APP_USER} + {DB_ADMIN_USER}, root = unix socket only')
    if bind not in ('127.0.0.1', 'localhost', '::1'):
        warn(f'MariaDB listens on {bind}. Keep port 3306 closed in the EC2 security group.')


def run_as_app(args, capture=False):
    py = os.path.join(APP_DIR, 'venv', 'bin', 'python')
    env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': STATE_DIR, 'LANG': 'C.UTF-8',
           'FLASK_ENV': 'production'}
    return sh([shutil.which('runuser') or '/usr/sbin/runuser', '-u', SVC_USER, '--', py, *args], env=env, capture=capture,
              check=False)


def migrate_and_init():
    step('Schema and data')
    marker = os.path.join(STATE_DIR, '.sqlite-migrated')
    sqlite_db = os.path.join(APP_DIR, 'instance', 'edgeshell.db')
    has_tables = mysql(f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{DB_NAME}' AND table_name='users'") == '1'
    users = int(mysql(f'SELECT COUNT(*) FROM {DB_NAME}.users') or 0) if has_tables else 0
    if os.path.isfile(sqlite_db) and not os.path.exists(marker) and users == 0:
        os.chdir(APP_DIR)
        r = run_as_app([os.path.join(APP_DIR, 'manage.py'), 'migrate-sqlite', sqlite_db], capture=True)
        out = (r.stdout or '') + (r.stderr or '')
        if r.returncode != 0 or 'MIGRATED' not in out:
            die('SQLite -> MariaDB migration failed:\n' + out[-3000:])
        counts = json.loads(out.split('MIGRATED', 1)[1].strip().splitlines()[0])
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        for ext in ('', '-wal', '-shm'):
            if os.path.isfile(sqlite_db + ext):
                os.replace(sqlite_db + ext, f'{sqlite_db}.migrated-{stamp}{ext}')
        write(marker, json.dumps({'at': stamp, 'counts': counts}) + '\n', 0o640, (SVC_USER, SVC_USER))
        ok('Migrated SQLite -> MariaDB: ' + ', '.join(f'{k} {v}' for k, v in counts.items() if v))
    elif users:
        ok(f'MariaDB already holds data ({users} user(s)); no migration needed.')
    os.chdir(APP_DIR)
    r = run_as_app([os.path.join(APP_DIR, 'manage.py'), 'seed'], capture=True)
    if r.returncode != 0:
        die('App could not initialise its schema on MariaDB:\n' + ((r.stdout or '') + (r.stderr or ''))[-3000:])
    tables = mysql(f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{DB_NAME}'")
    ok(f'Schema ready ({tables} tables and views).')


def tailscale_ip():
    if not shutil.which('tailscale'):
        return None, 'tailscale CLI not found'
    r = sh(['tailscale', 'ip', '-4'], check=False, capture=True)
    ip = (r.stdout or '').strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else None
    st = sh(['tailscale', 'status', '--json'], check=False, capture=True)
    try:
        state = json.loads(st.stdout).get('BackendState', 'unknown')
    except Exception:
        state = 'unknown'
    return ip, state


def bcrypt_hash(password):
    py = os.path.join(APP_DIR, 'venv', 'bin', 'python')
    return sh([py, '-c', 'import bcrypt,sys;print(bcrypt.hashpw(sys.stdin.read().encode(),'
               'bcrypt.gensalt(12)).decode())'], input_text=password, capture=True).stdout.strip()


def php_version():
    vs = sorted((d for d in os.listdir('/etc/php') if os.path.isdir(f'/etc/php/{d}/fpm')),
                key=lambda v: [int(x) for x in v.split('.')])
    if not vs:
        die('php-fpm not found under /etc/php/*/fpm')
    return vs[-1]


def setup_phpmyadmin(cfg, sec):
    step('phpMyAdmin')
    ts_ip, ts_state = tailscale_ip()
    cfg['ts_ip'], cfg['ts_state'] = ts_ip, ts_state
    if ts_ip:
        cfg['pma_bind'] = ts_ip
        ok(f'Tailscale {ts_state}, this host is {ts_ip}. phpMyAdmin binds there only.')
    else:
        cfg['pma_bind'] = '127.0.0.1'
        warn(f'Tailscale IP not found ({ts_state}). phpMyAdmin bound to 127.0.0.1; '
             f"reach it with: ssh -L {cfg['pma_port']}:127.0.0.1:{cfg['pma_port']} <ec2>")
    phpv = php_version()
    write(f'/etc/php/{phpv}/fpm/pool.d/edgeshell-pma.conf', tpl('pma-fpm-pool.conf'))
    write('/etc/phpmyadmin/conf.d/edgeshell.inc.php', tpl('pma-config.inc.php'), 0o640, ('root', 'www-data'))
    write('/etc/nginx/edgeshell/pma.htpasswd',
          f"{sec['PMA_BASIC_USER']}:{bcrypt_hash(sec['PMA_BASIC_PASS'])}\n", 0o640, ('root', 'www-data'))
    os.makedirs('/var/lib/phpmyadmin/tmp', exist_ok=True)
    shutil.chown('/var/lib/phpmyadmin/tmp', 'www-data', 'www-data')
    sh([f'php-fpm{phpv}', '-t'], quiet=True)
    systemctl('enable', f'php{phpv}-fpm')
    systemctl('restart', f'php{phpv}-fpm')
    cfg['php_version'] = phpv
    ok(f'PHP-FPM {phpv} pool edgeshell-pma (on-demand), hardened conf.d, basic-auth user {sec["PMA_BASIC_USER"]}')


def imds_public_ip():
    """EC2 public IP via IMDSv2 (best effort, 1 s timeouts)."""
    try:
        import urllib.request
        tok = urllib.request.urlopen(urllib.request.Request(
            'http://169.254.169.254/latest/api/token', method='PUT',
            headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'}), timeout=1).read().decode()
        return urllib.request.urlopen(urllib.request.Request(
            'http://169.254.169.254/latest/meta-data/public-ipv4',
            headers={'X-aws-ec2-metadata-token': tok}), timeout=1).read().decode().strip()
    except Exception:
        return None


def nginx_test_reload():
    sh(['nginx', '-t'], quiet=True)
    if NO_SYSTEMD:
        return
    systemctl('enable', 'nginx')
    active = systemctl('is-active', '--quiet', 'nginx', check=False).returncode == 0
    systemctl('reload' if active else 'start', 'nginx')


NGINX_MAIN = '/etc/nginx/nginx.conf'
_TOKENS_RE = re.compile(r'^(\s*)server_tokens\s+[^;]*;', re.M)


def nginx_common_conf():
    """EdgeShell's http-level settings without directives nginx.conf already
    sets. Ubuntu 26.04 ships an active `server_tokens build;` (24.04 had it
    commented out), and nginx rejects a duplicate. Hide the version either way:
    switch the distro line to `off`, or set it ourselves when it is absent."""
    conf = tpl('nginx-common.conf')
    try:
        main = open(NGINX_MAIN).read()
    except OSError:
        return conf
    if not _TOKENS_RE.search(main):
        return conf
    fixed = _TOKENS_RE.sub(r'\1server_tokens off;', main, count=1)
    if fixed != main:
        if not os.path.exists(NGINX_MAIN + '.edgeshell.bak'):
            shutil.copy2(NGINX_MAIN, NGINX_MAIN + '.edgeshell.bak')
        write(NGINX_MAIN, fixed)
        ok('nginx.conf: server_tokens set to off (original kept as nginx.conf.edgeshell.bak).')
    return '\n'.join(l for l in conf.splitlines() if not l.strip().startswith('server_tokens')) + '\n'


def setup_nginx(cfg):
    step('Nginx reverse proxy')
    # Let nginx bind the Tailscale IP even before tailscaled brings it up at boot.
    write('/etc/sysctl.d/60-edgeshell.conf',
          '# EdgeShell: allow nginx to bind the Tailscale IP before tailscaled is up\n'
          'net.ipv4.ip_nonlocal_bind = 1\n')
    sh(['sysctl', '-q', '-p', '/etc/sysctl.d/60-edgeshell.conf'], check=False, quiet=True)

    for f in ('/etc/nginx/sites-enabled/default',):
        if os.path.lexists(f):
            os.remove(f)
    os.makedirs(ACME_ROOT, exist_ok=True)
    domain = cfg['domain']
    cert = f'/etc/letsencrypt/live/{domain}/fullchain.pem' if domain else None

    write('/etc/nginx/conf.d/edgeshell-common.conf', nginx_common_conf())
    write('/etc/nginx/snippets/edgeshell-proxy.conf', tpl('nginx-proxy.conf', PORT=cfg['port']))

    def apply(tls):
        hsts = ('add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'
                if tls else '# HSTS enabled automatically once TLS is active')
        write('/etc/nginx/snippets/edgeshell-app-locations.conf', tpl('nginx-app-locations.conf', HSTS=hsts))
        if not domain:
            site = tpl('nginx-site-nodomain.conf')
        elif tls:
            site = tpl('nginx-site-tls.conf', DOMAIN=domain)
        else:
            site = tpl('nginx-site-http.conf', DOMAIN=domain)
        write('/etc/nginx/sites-available/edgeshell.conf', site)
        write('/etc/nginx/sites-available/edgeshell-pma.conf',
              tpl('nginx-pma.conf', PMA_BIND=cfg['pma_bind'], PMA_PORT=cfg['pma_port']))
        for n in ('edgeshell.conf', 'edgeshell-pma.conf'):
            link = f'/etc/nginx/sites-enabled/{n}'
            if not os.path.lexists(link):
                os.symlink(f'/etc/nginx/sites-available/{n}', link)
        nginx_test_reload()

    tls = bool(cert and os.path.isfile(cert))
    apply(tls)
    if not domain:
        warn('No EDGESHELL_DOMAIN set: the app is served on the raw IP over HTTP. '
             'Set EDGESHELL_DOMAIN (+ LETSENCRYPT_EMAIL) in .env and re-run to hide it.')
    elif not tls and cfg['email']:
        pub = imds_public_ip()
        try:
            resolved = socket.gethostbyname(domain)
        except Exception:
            resolved = None
        if pub and resolved and resolved != pub:
            warn(f'{domain} resolves to {resolved}, this instance is {pub} (fine if behind a CDN proxy).')
        elif not resolved:
            warn(f'{domain} does not resolve yet; certificate request will likely fail.')
        r = sh(['certbot', 'certonly', '--webroot', '-w', ACME_ROOT, '-d', domain,
                '--email', cfg['email'], '--agree-tos', '--no-eff-email',
                '--non-interactive', '--keep-until-expiring'], check=False, capture=True)
        if r.returncode == 0 and os.path.isfile(cert):
            tls = True
            apply(True)
            ok(f"Let's Encrypt certificate issued for {domain}.")
        else:
            warn('Certificate request failed; staying on HTTP. Check DNS A record and that port 80 '
                 'is open in the security group, then re-run.\n     ' + (r.stderr or r.stdout or '').strip()[-600:])
    elif not tls:
        warn('No LETSENCRYPT_EMAIL: domain served over HTTP only.')
    if tls:
        write('/etc/letsencrypt/renewal-hooks/deploy/edgeshell-nginx.sh',
              '#!/bin/sh\nsystemctl reload nginx\n', 0o755)
        if not NO_SYSTEMD:
            systemctl('enable', '--now', 'certbot.timer', check=False)
    cfg['tls'] = tls
    scheme = 'https' if tls else 'http'
    ok(f"Public: {scheme}://{domain or '<ip>'}  (raw IP / unknown host -> dropped)" if domain
       else 'Public: http://<ec2-ip>')


def ssh_ports():
    """Ports sshd listens on, so the firewall can never lock the admin out."""
    if not shutil.which('sshd'):
        return [22]
    r = sh(['sshd', '-T'], check=False, capture=True)
    ports = {int(l.split()[1]) for l in (r.stdout or '').splitlines()
             if l.startswith('port ') and l.split()[1].isdigit()}
    return sorted(ports) or [22]


def setup_firewall(cfg):
    step('Host firewall')
    cfg['firewall'] = False
    if FIREWALL == 'off':
        warn('EDGESHELL_FIREWALL=off: host firewall left unchanged; the security group is the only filter.')
        return
    if not shutil.which('ufw'):
        warn('ufw is not available; host firewall not configured. Rely on the security group.')
        return
    ssh = ssh_ports()
    rules = [['default', 'deny', 'incoming'], ['default', 'allow', 'outgoing'],
             *[['allow', f'{p}/tcp', 'comment', 'ssh'] for p in ssh],
             ['allow', '80/tcp', 'comment', 'http: certificates and redirect'],
             ['allow', '443/tcp', 'comment', 'EdgeShell https'],
             ['allow', f'{TAILSCALE_UDP}/udp', 'comment', 'tailscale direct'],
             ['allow', 'in', 'on', 'tailscale0', 'comment', 'tailnet: phpMyAdmin, admin']]
    for rule in rules:
        sh(['ufw', *rule], check=False, quiet=True)
    r = sh(['ufw', '--force', 'enable'], check=False, quiet=True)
    if r.returncode != 0:
        warn('ufw could not be enabled: ' + (r.stderr or r.stdout or '').strip()[-300:])
        return
    cfg['firewall'] = True
    ok(f"ufw on: inbound allowed only for ssh {', '.join(map(str, ssh))}/tcp, 80/tcp, 443/tcp, "
       f"{TAILSCALE_UDP}/udp and the tailscale0 interface. Everything else is denied.")


def _private_bind(host):
    """Loopback or Tailscale (CGNAT 100.64/10, fd7a:115c:a1e0::/48) addresses."""
    import ipaddress
    h = host.strip('[]').split('%')[0]
    if h in ('', '*', '0.0.0.0', '::'):
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (ip.is_loopback or ip in ipaddress.ip_network('100.64.0.0/10')
            or ip in ipaddress.ip_network('fd7a:115c:a1e0::/48'))


def audit_ports(cfg):
    """List every listening socket and flag anything reachable beyond
    loopback or the tailnet that is not ssh, http(s) or Tailscale."""
    step('Exposure audit')
    if not shutil.which('ss'):
        warn('ss not found; exposure audit skipped.')
        return
    r = sh(['ss', '-H', '-tulpn'], check=False, capture=True)
    ssh = set(ssh_ports())
    exposed = set()
    for line in (r.stdout or '').splitlines():
        f = line.split()
        if len(f) < 5 or f[0] not in ('tcp', 'udp'):
            continue
        host, _, port = f[4].rpartition(':')
        if not port.isdigit() or _private_bind(host):
            continue
        port = int(port)
        if (f[0], port) in ALLOWED_PUBLIC or (f[0] == 'tcp' and port in ssh):
            continue
        proc = re.search(r'users:\(\("([^"]+)"', line)
        exposed.add(f"{f[0]}/{port} on {host or '*'} ({proc.group(1) if proc else 'unknown'})")
    if exposed:
        guard = 'ufw already denies them' if cfg.get('firewall') else 'block them in the security group'
        warn('Listening beyond loopback/tailnet: ' + '; '.join(sorted(exposed)) + f' ({guard}).')
    else:
        ok(f"Only ssh, 80 and 443 (plus Tailscale) listen publicly. App {cfg['port']}, MariaDB 3306 "
           f"and phpMyAdmin {cfg['pma_port']} are loopback or tailnet only.")


def install_units(cfg):
    step('systemd services')
    if NO_SYSTEMD:
        ok('Skipped (EDGESHELL_NO_SYSTEMD=1).')
        return
    for unit in ('edgeshell.service', 'edgeshell-watchdog.service',
                 'edgeshell-heartbeat.service', 'edgeshell-heartbeat.timer'):
        write(f'/etc/systemd/system/{unit}',
              tpl(os.path.join('..', 'systemd', unit), APP_DIR=APP_DIR, PORT=cfg['port']))
    systemctl('daemon-reload')
    systemctl('enable', 'edgeshell.service', 'edgeshell-watchdog.service', 'edgeshell-heartbeat.timer')
    ver = open(os.path.join(APP_DIR, 'VERSION')).read().strip()
    write(os.path.join(STATE_DIR, 'restart-reason.json'),
          json.dumps({'reason': 'deploy', 'detail': f'EdgeShell {ver} installed/updated by run.py'}) + '\n',
          0o640, (SVC_USER, SVC_USER))
    systemctl('reset-failed', 'edgeshell', check=False)
    systemctl('restart', 'edgeshell.service')
    systemctl('restart', 'edgeshell-watchdog.service')
    systemctl('start', 'edgeshell-heartbeat.timer')
    ok('edgeshell, edgeshell-watchdog, edgeshell-heartbeat.timer enabled (start on boot).')


def wait_healthy(cfg):
    step('Health check')
    if NO_SYSTEMD:
        ok('Skipped (no systemd).')
        return True
    import urllib.request
    url = f"http://127.0.0.1:{cfg['port']}/healthz"
    for _ in range(45):
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                body = json.loads(r.read().decode())
                ok(f"{url} -> {body.get('status')} {body.get('checks')}")
                return True
        except Exception:
            time.sleep(2)
    warn('App did not become healthy in 90 s. See: journalctl -u edgeshell -n 100 --no-pager')
    return False


def report(cfg, sec, healthy):
    scheme = 'https' if cfg.get('tls') else 'http'
    public = f"{scheme}://{cfg['domain']}" if cfg['domain'] else 'http://<ec2-public-ip>'
    pma = f"http://{cfg['pma_bind']}:{cfg['pma_port']}/"
    lines = [
        f"EdgeShell credentials ({datetime.now():%Y-%m-%d %H:%M %Z}). Keep this file root-only.",
        '',
        f'App URL               {public}',
        'App first login       admin / admin123 (forced change on first login) unless migrated',
        '',
        f'phpMyAdmin URL        {pma}   (Tailscale only)' if cfg.get('ts_ip') else
        f'phpMyAdmin URL        {pma}   (via: ssh -L {cfg["pma_port"]}:127.0.0.1:{cfg["pma_port"]} <ec2>)',
        f'  step 1 browser auth {sec["PMA_BASIC_USER"]} / {sec["PMA_BASIC_PASS"]}',
        f'  step 2 DB login     {DB_ADMIN_USER} / {sec["DB_ADMIN_PASS"]}',
        '',
        f'App DB user           {DB_APP_USER} (password in {APP_DIR}/.env)',
        'MariaDB root          sudo mariadb   (unix socket only, no password login)',
        '',
        'Useful views          v_activity_local, v_service_events_local, v_user_sessions',
    ]
    write(CRED_REPORT, '\n'.join(lines) + '\n', 0o600, ('root', 'root'))

    rows = [
        ('App', public, 'Gunicorn 127.0.0.1:' + cfg['port'] + ' behind Nginx'),
        ('phpMyAdmin', pma, 'tailnet only + basic auth + DB login'),
        ('Database', f'MariaDB `{DB_NAME}`', 'socket, UTC timestamps'),
        ('Service', 'systemctl status edgeshell', 'auto-start on boot, auto-restart'),
        ('Logs', 'journalctl -u edgeshell -f', 'restarts also in service_events'),
        ('Credentials', CRED_REPORT, 'root only'),
    ]
    print(f"\n  {C['B']}{'Component':<12} {'Where':<44} Notes{C['x']}")
    for a, b, c in rows:
        print(f'  {a:<12} {b:<44} {c}')
    print('\n  Security group (set in AWS; run.py cannot change it): allow 80 + 443 from anywhere,')
    print(f"  22 from your IP only (or none with Tailscale SSH), optional {TAILSCALE_UDP}/udp.")
    print(f"  Do NOT open {cfg['port']}, {cfg['pma_port']} or 3306."
          + ('  The host firewall (ufw) enforces the same rules.' if cfg.get('firewall') else ''))
    warns = [m for t, m in STEPS if t == 'warn']
    if warns:
        print(f"\n  {C['y']}Warnings:{C['x']}")
        for w in warns:
            print(f'   - {w}')
    print(f"\n  {C['g'] if healthy else C['y']}{'EdgeShell is live.' if healthy else 'Installed with warnings.'}{C['x']}"
          f'  Re-run  sudo python3 run.py  any time to update.\n')


def main(args=None):
    args = args or {}
    print(f"\n  {C['B']}EdgeShell production setup{C['x']}\n  " + '─' * 40)
    preflight()
    cfg = load_settings(args)
    apt_install(cfg)
    set_timezone(cfg)
    ensure_user()
    sync_code()
    sec = load_secrets()
    write_env(cfg, sec, cookie_secure=False)
    build_venv()
    setup_mariadb(sec)
    migrate_and_init()
    setup_phpmyadmin(cfg, sec)
    setup_nginx(cfg)
    setup_firewall(cfg)
    write_env(cfg, sec, cookie_secure=cfg['tls'])
    install_units(cfg)
    healthy = wait_healthy(cfg)
    audit_ports(cfg)
    report(cfg, sec, healthy)


if __name__ == '__main__':
    main({'yes': '--yes' in sys.argv})
