"""EdgeShell configuration.

Security posture:
  - In production, SECRET_KEY and CREDENTIAL_KEY MUST be supplied via the
    environment. There are no usable fallbacks; the app refuses to start
    without them. This prevents shipping a build that encrypts fleet SSH
    credentials with a publicly known key.
  - CSRF protection is ON. State-changing requests require a token.
  - Session cookies are hardened; SECURE is enabled in production (needs TLS
    at the reverse proxy / load balancer).
"""
import os
import tempfile
from datetime import timedelta
from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

_DEV_SECRET = 'edgeshell-dev-only-not-for-production'
_SQLITE_PREFIX = 'sqlite:///'


def _read_version():
    try:
        with open(os.path.join(BASE_DIR, 'VERSION'), encoding='utf-8') as f:
            return f.read().strip() or 'dev'
    except OSError:
        return os.environ.get('EDGESHELL_VERSION', 'dev')


def _env_bool(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ('1', 'true', 'yes', 'on')


def _normalize_db_uri(uri):
    """Anchor relative SQLite paths to BASE_DIR so the DB opens from any CWD.

    A bare 'sqlite:///instance/edgeshell.db' is relative to the working
    directory, which breaks when the app is launched (or re-exec'd, as run.py
    does on Windows) from elsewhere. Rewrite it to an absolute path under the
    project directory. Non-SQLite URLs (Postgres, MariaDB) pass through.
    """
    if not uri or not uri.startswith(_SQLITE_PREFIX):
        return uri
    path = uri[len(_SQLITE_PREFIX):]
    if path in (':memory:', ''):
        return uri
    is_abs = os.path.isabs(path) or (len(path) > 1 and path[1] == ':')  # C:\ etc
    if not is_abs:
        path = os.path.join(BASE_DIR, path)
    return _SQLITE_PREFIX + os.path.abspath(path).replace('\\', '/')


def _sqlite_path(uri):
    """Return the filesystem path for a sqlite:/// URI, else None."""
    if uri and uri.startswith(_SQLITE_PREFIX):
        p = uri[len(_SQLITE_PREFIX):]
        if p not in (':memory:', ''):
            return p
    return None


class Config:
    VERSION = _read_version()

    # Populated per-environment below.
    SECRET_KEY = None
    CREDENTIAL_KEY = None

    SQLALCHEMY_DATABASE_URI = _normalize_db_uri(
        os.environ.get('DATABASE_URL')
        or f'sqlite:///{os.path.join(BASE_DIR, "instance", "edgeshell.db")}'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # pool_recycle keeps pooled MariaDB connections younger than wait_timeout.
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 280}

    # Time. DB always stores naive UTC; pages render in DISPLAY_TZ.
    DISPLAY_TZ = os.environ.get('DISPLAY_TZ') or os.environ.get('TZ') or 'Asia/Kolkata'

    # Reverse proxy (Nginx). Trust one hop of X-Forwarded-For/Proto so client
    # IPs are real (lockout, rate limits, audit) instead of 127.0.0.1.
    BEHIND_PROXY = _env_bool('BEHIND_PROXY', False)

    # Per-user session limits (users.max_sessions, 1-4, default 1). Signing in
    # past the limit signs the oldest device out. false lifts the limit; sessions
    # are still tracked server-side and revoked on logout or password reset.
    SINGLE_SESSION = _env_bool('SINGLE_SESSION', True)

    # Inactivity: after IDLE_MINUTES without a click, key press or page load a
    # session shows a 'still there?' prompt and is signed out unless answered
    # within IDLE_WARNING_MINUTES. IDLE_MINUTES=0 turns this off.
    IDLE_MINUTES = int(os.environ.get('IDLE_MINUTES', '10'))
    IDLE_WARNING_MINUTES = int(os.environ.get('IDLE_WARNING_MINUTES', '5'))

    # Two-factor authentication, mandatory for every account. Codes are checked
    # by privacyIDEA (its own container in the Docker stack) when
    # PRIVACYIDEA_URL is set, else by the built-in verifier (MFA_PROVIDER=local).
    MFA_REQUIRED = _env_bool('MFA_REQUIRED', True)
    MFA_PROVIDER = os.environ.get('MFA_PROVIDER', '')
    MFA_ISSUER = os.environ.get('MFA_ISSUER', 'EdgeShell')
    PRIVACYIDEA_URL = os.environ.get('PRIVACYIDEA_URL', '')
    PRIVACYIDEA_ADMIN = os.environ.get('PRIVACYIDEA_ADMIN', 'edgeshell')
    PRIVACYIDEA_PASSWORD = os.environ.get('PRIVACYIDEA_PASSWORD', '')
    PRIVACYIDEA_TIMEOUT = float(os.environ.get('PRIVACYIDEA_TIMEOUT', '8'))
    PRIVACYIDEA_VERIFY_TLS = _env_bool('PRIVACYIDEA_VERIFY_TLS', True)

    # Accounts. Self sign-up creates a pending account an administrator
    # approves with a role. Reset links for administrators are emailed and
    # point at PUBLIC_URL (never built from the request's Host header).
    SIGNUP_ENABLED = _env_bool('SIGNUP_ENABLED', True)
    PUBLIC_URL = os.environ.get('PUBLIC_URL', '').rstrip('/')
    RESET_LINK_MINUTES = int(os.environ.get('RESET_LINK_MINUTES', '30'))

    # Outgoing mail for reset links (any SMTP server: Gmail app password, SES,
    # Brevo, ...). Without it administrators recover with a backup code.
    SMTP_HOST = os.environ.get('SMTP_HOST', '')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
    SMTP_USER = os.environ.get('SMTP_USER', '')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
    SMTP_FROM = os.environ.get('SMTP_FROM', '')
    SMTP_SECURITY = os.environ.get('SMTP_SECURITY', 'starttls').lower()  # starttls|ssl|none

    # CSRF (Flask-WTF)
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None  # tie token life to the session, not a fixed clock

    # Session
    PERMANENT_SESSION_LIFETIME = timedelta(
        hours=int(os.environ.get('SESSION_HOURS', '8'))
    )
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    # Secure cookies require HTTPS. Over plain HTTP (LAN IP, bring-up) the
    # browser drops a Secure cookie except on localhost, which breaks login
    # with 'CSRF session token is missing'. Default False; enable behind TLS
    # or set SESSION_COOKIE_SECURE=true in .env.
    SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', False)

    # Login lockout
    MAX_FAILED_LOGINS = int(os.environ.get('MAX_FAILED_LOGINS', '5'))
    LOCKOUT_MINUTES = int(os.environ.get('LOCKOUT_MINUTES', '15'))

    # Upload
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
    ANSIBLE_INVENTORY_DIR = os.path.join(BASE_DIR, 'ansible', 'inventory')
    ANSIBLE_PLAYBOOK_DIR = os.path.join(BASE_DIR, 'ansible', 'playbooks')

    # Rate limiting. Use RATELIMIT_STORAGE_URI=redis://... behind >1 worker.
    # Dashboards poll every 15-30 s, so the default must sit well above that.
    RATELIMIT_DEFAULT = os.environ.get('RATELIMIT_DEFAULT', '20000 per day;2000 per hour')
    RATELIMIT_STORAGE_URI = os.environ.get('RATELIMIT_STORAGE_URI', 'memory://')

    # Fleet / integrations
    ORGUI_REPO = os.environ.get(
        'ORGUI_REPO', 'https://github.com/855princekumar/OpenRemoteGUI.git'
    )
    ORGUI_DEFAULT_PORT = int(os.environ.get('ORGUI_DEFAULT_PORT', '6080'))
    NODERED_DEFAULT_PORT = int(os.environ.get('NODERED_DEFAULT_PORT', '1880'))
    VPN_BACKEND = os.environ.get('VPN_BACKEND', 'tailscale')  # tailscale | wireguard
    WIREGUARD_INTERFACE = os.environ.get('WIREGUARD_INTERFACE', 'wg0')
    METRICS_RETENTION_DAYS = int(os.environ.get('METRICS_RETENTION_DAYS', '14'))

    # Email (useSend, open-source Resend-compatible). Best-effort; ticket
    # actions never fail if this is off or misconfigured.
    EMAIL_ENABLED = _env_bool('EMAIL_ENABLED', False)
    USESEND_BASE_URL = os.environ.get('USESEND_BASE_URL', 'https://app.usesend.com')
    USESEND_API_KEY = os.environ.get('USESEND_API_KEY', '')
    USESEND_FROM = os.environ.get('USESEND_FROM', '')

    @staticmethod
    def init_app(app):
        dirs = [os.path.join(BASE_DIR, 'instance'), Config.UPLOAD_FOLDER,
                Config.ANSIBLE_INVENTORY_DIR, Config.ANSIBLE_PLAYBOOK_DIR]
        # Ensure the SQLite file's own directory exists (handles custom paths).
        sqlite_path = _sqlite_path(app.config.get('SQLALCHEMY_DATABASE_URI'))
        if sqlite_path:
            dirs.append(os.path.dirname(sqlite_path))
        for d in dirs:
            if d:
                os.makedirs(d, exist_ok=True)


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', False)
    SECRET_KEY = os.environ.get('SECRET_KEY') or _DEV_SECRET
    CREDENTIAL_KEY = os.environ.get('CREDENTIAL_KEY') or _DEV_SECRET


class ProductionConfig(Config):
    DEBUG = False
    # True behind HTTPS (recommended). Set SESSION_COOKIE_SECURE=false in .env
    # only for temporary HTTP bring-up before TLS is in front.
    SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', True)

    @staticmethod
    def init_app(app):
        Config.init_app(app)
        missing = [k for k in ('SECRET_KEY', 'CREDENTIAL_KEY')
                   if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                'Refusing to start in production without: '
                + ', '.join(missing)
                + '. Generate with: '
                'python -c "import secrets; print(secrets.token_hex(32))"'
            )


# Bind secrets for production from the environment (validated in init_app).
ProductionConfig.SECRET_KEY = os.environ.get('SECRET_KEY')
ProductionConfig.CREDENTIAL_KEY = os.environ.get('CREDENTIAL_KEY')


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    # A file, not ':memory:': an in-memory SQLite database shares one
    # connection across threads, so background jobs in tests behaved unlike
    # any real install (MariaDB or file SQLite give each session its own).
    SQLALCHEMY_DATABASE_URI = os.environ.get('TEST_DATABASE_URL') or _normalize_db_uri(
        _SQLITE_PREFIX + os.path.join(tempfile.gettempdir(), f'edgeshell-test-{os.getpid()}.db'))
    SECRET_KEY = 'testing'
    CREDENTIAL_KEY = 'testing-credential-key'
    # Tests that exercise 2FA turn it on; the rest sign in with a password.
    MFA_REQUIRED = False
    MFA_PROVIDER = 'local'
    PRIVACYIDEA_URL = ''
    IDLE_MINUTES = 10
    SMTP_HOST = ''


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig,
}
