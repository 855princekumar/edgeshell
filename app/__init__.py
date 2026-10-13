import sqlite3
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from flask_sock import Sock
from sqlalchemy import event
from sqlalchemy.engine import Engine
from config import config

db = SQLAlchemy()
login_manager = LoginManager()
bcrypt = Bcrypt()
limiter = Limiter(key_func=get_remote_address)
csrf = CSRFProtect()
sock = Sock()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    # SQLite-only. No-op on Postgres/MariaDB, so the migration path is clean.
    if isinstance(dbapi_connection, sqlite3.Connection):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def create_app(config_name='default'):
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    config[config_name].init_app(app)

    db.init_app(app)
    login_manager.init_app(app)
    bcrypt.init_app(app)
    limiter.init_app(app)
    csrf.init_app(app)
    # WebSocket terminals: ping every 25 s so proxies keep idle shells open.
    app.config.setdefault('SOCK_SERVER_OPTIONS', {'ping_interval': 25})
    sock.init_app(app)

    # Rate-limit storage from config (Redis recommended behind >1 worker).
    app.config.setdefault('RATELIMIT_STORAGE_URI',
                          app.config.get('RATELIMIT_STORAGE_URI', 'memory://'))

    if app.config.get('BEHIND_PROXY'):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _register_time_filters(app)

    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access EdgeShell.'
    login_manager.login_message_category = 'warning'

    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.nodes import nodes_bp
    from app.routes.terminal import terminal_bp
    from app.routes.files import files_bp
    from app.routes.logs import logs_bp
    from app.routes.settings import settings_bp
    from app.routes.users import users_bp
    from app.routes.api import api_bp
    from app.routes.tailscale import tailscale_bp
    from app.routes.monitoring import monitoring_bp
    from app.routes.vpn import vpn_bp
    from app.routes.health import health_bp
    from app.routes.issues import issues_bp
    from app.routes.account import account_bp
    from app.routes import shell as _shell  # noqa: F401  (registers the WebSocket route)

    for bp in (auth_bp, dashboard_bp, nodes_bp, terminal_bp, files_bp, logs_bp,
               settings_bp, users_bp, api_bp, tailscale_bp, monitoring_bp,
               vpn_bp, health_bp, issues_bp, account_bp):
        app.register_blueprint(bp)

    # JSON API and health checks are token/session protected but not CSRF-form
    # based, so exempt them from CSRF (they still require auth).
    csrf.exempt(api_bp)
    csrf.exempt(health_bp)

    @app.context_processor
    def _inject_defaults():
        return {
            'ORGUI_DEFAULT_PORT': app.config['ORGUI_DEFAULT_PORT'],
            'NODERED_DEFAULT_PORT': app.config['NODERED_DEFAULT_PORT'],
            'VPN_BACKEND': app.config['VPN_BACKEND'],
            'SIGNUP_ENABLED': app.config.get('SIGNUP_ENABLED', True),
        }

    from app.services.session_service import register_single_session
    register_single_session(app)

    # Import models so their tables are registered before create_all.
    from app.models import issue as _issue_models  # noqa: F401
    from app.models import ServiceEvent as _svc  # noqa: F401
    with app.app_context():
        db.create_all()
        _ensure_schema()
        _ensure_views(app)
        _seed_defaults()
        _normalize_roles()
        from app.services.account_service import ensure_owner
        ensure_owner()
        _one_time_fixes()

    return app


def _register_time_filters(app):
    """'{{ ts|localtime }}' renders a naive-UTC DB value in DISPLAY_TZ."""
    from app.utils.timeutil import to_local, tz_label
    tz_name = app.config.get('DISPLAY_TZ', 'UTC')

    @app.template_filter('localtime')
    def _localtime(dt, fmt='%d %b %Y %H:%M'):
        if not dt:
            return ''
        return to_local(dt, tz_name).strftime(fmt)

    @app.context_processor
    def _inject_tz():
        return {'DISPLAY_TZ': tz_name, 'TZ_LABEL': tz_label(tz_name)}


def _ensure_views(app):
    """MariaDB convenience views showing UTC columns in DISPLAY_TZ, so rows
    read naturally in phpMyAdmin. Tables themselves stay UTC."""
    from sqlalchemy import text
    from datetime import datetime
    from app.utils.timeutil import get_tz
    bind = db.engine
    if bind.dialect.name != 'mysql':
        return
    off = datetime.now(get_tz(app.config.get('DISPLAY_TZ'))).utcoffset()
    mins = int(off.total_seconds() // 60) if off else 0
    sign = '+' if mins >= 0 else '-'
    tz = f"{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"
    views = {
        'v_activity_local': (
            f"SELECT id, CONVERT_TZ(timestamp,'+00:00','{tz}') AS local_time, "
            "user, node, action, status, ip_address, duration, command "
            "FROM activity_logs"),
        'v_service_events_local': (
            f"SELECT id, CONVERT_TZ(timestamp,'+00:00','{tz}') AS local_time, "
            "event, reason, detail, result, exit_code, exit_status, hostname, "
            "pid, version, host_uptime_s, boot_id FROM service_events"),
        'v_user_sessions': (
            "SELECT s.id, u.username, u.role, u.is_active, u.max_sessions, "
            "s.ip AS session_ip, s.agent AS session_agent, "
            f"CONVERT_TZ(s.started_at,'+00:00','{tz}') AS session_started, "
            f"CONVERT_TZ(s.seen_at,'+00:00','{tz}') AS session_last_seen, "
            f"CONVERT_TZ(s.revoked_at,'+00:00','{tz}') AS revoked_at, "
            "s.revoked_reason, s.device_type, "
            f"CONVERT_TZ(s.active_at,'+00:00','{tz}') AS last_interaction "
            "FROM user_sessions s JOIN users u ON u.id = s.user_id"),
    }
    try:
        with bind.begin() as conn:
            for name, sql in views.items():
                conn.execute(text(f'CREATE OR REPLACE VIEW {name} AS {sql}'))
    except Exception as e:  # views are a convenience; never block startup
        app.logger.warning('Could not create MariaDB views: %s', e)


def _normalize_roles():
    """Legacy data used capitalized roles (Administrator/Operator/Monitor).
    Standardize to lowercase so permission checks are consistent."""
    from app.models.user import User
    changed = False
    for u in User.query.all():
        norm = (u.role or '').strip().lower()
        if norm not in ('administrator', 'operator', 'monitor'):
            norm = 'monitor'
        if norm != u.role:
            u.role = norm
            changed = True
    if changed:
        db.session.commit()


def _ensure_schema():
    """Add columns introduced after a DB was first created (SQLite only).

    create_all() never alters existing tables, so a database made by an earlier
    build lacks newer columns. This adds any missing ones idempotently. For
    Postgres use a real migration; this is a no-op there. SQLite and
    MariaDB are handled (plain ADD COLUMN works on both).
    """
    from sqlalchemy import inspect, text
    bind = db.engine
    if bind.dialect.name not in ('sqlite', 'mysql'):
        return
    inspector = inspect(bind)
    wanted = {
        'nodes': {
            'arch': 'VARCHAR(32)',
            'nodered_port': 'INTEGER DEFAULT 1880',
            'orgui_installed': 'BOOLEAN',
            'orgui_port': 'INTEGER DEFAULT 6080',
            'orgui_checked_at': 'DATETIME',
            'is_deleted': 'BOOLEAN DEFAULT 0',
            'deleted_at': 'DATETIME',
            'docker_installed': 'BOOLEAN',
            'nodered_installed': 'BOOLEAN',
            'capabilities_checked_at': 'DATETIME',
            'last_error': 'VARCHAR(256)',
        },
        'users': {
            # Sessions moved to the user_sessions table (created by
            # create_all). Old session_* columns on existing DBs are unused.
            'max_sessions': 'INTEGER NOT NULL DEFAULT 1',
            'approval_status': "VARCHAR(16) NOT NULL DEFAULT 'approved'",
            'is_owner': 'BOOLEAN NOT NULL DEFAULT 0',
            'admin_since': 'DATETIME',
        },
        'user_sessions': {
            'device_type': 'VARCHAR(16)',
            'active_at': 'DATETIME',
        },
        'activity_logs': {
            'device': 'VARCHAR(160)',
        },
        'system_metrics': {
            'status': 'VARCHAR(16)',
        },
    }
    for table, cols in wanted.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c['name'] for c in inspector.get_columns(table)}
        with bind.begin() as conn:
            for col, ddl in cols.items():
                if col not in existing:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}'))

    # Repair role casing: permission checks are lowercase, but an older user-add
    # form saved 'Administrator'/'Operator'/'Monitor'. Normalize so an operator
    # is not silently demoted to read-only.
    if 'users' in inspector.get_table_names():
        with bind.begin() as conn:
            conn.execute(text("UPDATE users SET role = LOWER(role) "
                              "WHERE role <> LOWER(role)"))


def _one_time_fixes():
    """Data repairs that must run once per database, recorded in settings."""
    from app.models import Settings
    from app.models.node import Node
    done = {r.key for r in Settings.query.filter(Settings.key.like('fix_%')).all()}
    if 'fix_orgui_detection_v2' not in done:
        # Older scans counted Raspberry Pi OS's own wayvnc as OpenRemoteGUI, so
        # every reachable Pi was marked installed. Forget those results; the
        # metrics collector re-detects it correctly on its next pass.
        Node.query.update({Node.orgui_installed: None, Node.orgui_checked_at: None},
                          synchronize_session=False)
        db.session.add(Settings(key='fix_orgui_detection_v2', value='done'))
        db.session.commit()


def _seed_defaults():
    from app.models.user import User
    from app.models.settings import Settings

    # Only into an empty database: never re-create a default admin/admin123
    # because the original admin was renamed or removed.
    if User.query.first() is None:
        from datetime import datetime
        admin = User(username='admin', email='admin@edgeshell.local',
                     role='administrator', must_change_password=True,
                     is_active=True, max_sessions=1, is_owner=True,
                     admin_since=datetime.utcnow())
        admin.set_password('admin123')
        db.session.add(admin)

    if not Settings.query.first():
        defaults = [
            ('platform_name', 'EdgeShell'),
            ('session_timeout', '60'),
            ('default_ssh_port', '22'),
            ('ssh_timeout', '10'),
            ('metrics_interval', '30'),
            ('ansible_enabled', 'true'),
            ('audit_all_commands', 'true'),
            ('require_sudo_confirm', 'false'),
            ('vpn_backend', 'tailscale'),
            ('blocked_commands', 'rm -rf /\nmkfs\n:(){ :|:& };:'),
        ]
        for key, value in defaults:
            db.session.add(Settings(key=key, value=value))

    db.session.commit()
