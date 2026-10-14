from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from app import db
from app.models import Settings, ActivityLog
from app.models.user import User
from app.models.node import Node
from functools import wraps

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash('Administrator access required.', 'danger')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


def get_all_settings():
    """Return all Settings rows as a flat dict keyed by setting key."""
    rows = Settings.query.all()
    return {r.key: r.value for r in rows} if rows else {}


def save_setting(key, value):
    row = Settings.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        db.session.add(Settings(key=key, value=str(value)))


@settings_bp.route('/', methods=['GET', 'POST'])
@login_required
@admin_required
def index():
    if request.method == 'POST':
        section = request.form.get('section', 'general')

        if section == 'general':
            for key in ['platform_name', 'platform_url', 'session_timeout',
                        'default_ssh_port', 'ssh_timeout', 'metrics_interval']:
                val = request.form.get(key)
                if val is not None:
                    save_setting(key, val)

        elif section == 'security':
            val = request.form.get('blocked_commands')
            if val is not None:
                lines = [ln.strip()[:200] for ln in val.splitlines() if ln.strip()][:200]
                save_setting('blocked_commands', '\n'.join(lines))
                from app.services.activity_service import log_activity
                log_activity('Blocked commands changed', response=f'{len(lines)} rule(s)', status='success')

        elif section == 'hot_retention':
            from app.services import lake
            from app.services.activity_service import log_activity
            period = request.form.get('period')
            if period not in lake.PERIODS:
                flash('Choose week, month, quarter, semester or year.', 'danger')
                return redirect(url_for('settings.index') + '#data-lake')
            enabled = bool(request.form.get('enabled'))
            lake.save_retention_policy(enabled, period)
            log_activity('Hot retention policy changed', status='warning' if enabled else 'success',
                         response=(f'on: keep one {period} on the hot database' if enabled else 'off')
                         + ('' if lake.configured() else ' (no data lake configured: nothing will be deleted)'))
            flash('Hot retention ' + (f'on: the hot database keeps one {period}.' if enabled else 'off.'), 'success')
            return redirect(url_for('settings.index') + '#data-lake')

        elif section == 'ansible':
            for key in ['ansible_playbooks_dir', 'ansible_inventory_dir',
                        'default_remote_user', 'upload_staging_dir']:
                val = request.form.get(key)
                if val is not None:
                    save_setting(key, val)
            save_setting('ansible_enabled', 'true' if request.form.get('ansible_enabled') else 'false')

        elif section in ('clear_logs', 'clear_node_logs'):
            # Clears the hot database only: the data lake keeps every entry.
            from app.services import lake
            from app.services.activity_service import log_activity
            table = 'activity_logs' if section == 'clear_logs' else 'node_logs'
            # Bandit B608 reviewed: the table name is one of two fixed values chosen in code, never from input
            removed = db.session.execute(db.text(f'SELECT COUNT(*) FROM {table}')).scalar() or 0  # nosec B608
            try:
                # Bandit B608 reviewed: the table name is one of two fixed values chosen in code, never from input
                lake.hot_only([(f'DELETE FROM {table}', {})])  # nosec B608
            except lake.LakeNotReady as e:
                flash(f'Not cleared: the data lake must be caught up first ({e}).', 'danger')
                return redirect(url_for('settings.index'))
            log_activity('Activity log cleared' if section == 'clear_logs' else 'Node logs cleared',
                         response=f'{removed} entries removed from the hot database by {current_user.username}'
                         + (' (kept in the data lake)' if lake.configured() else ''), status='warning')
            flash(('Activity' if section == 'clear_logs' else 'Node') + ' logs cleared'
                  + (' from the hot database; the data lake keeps them.' if lake.configured() else '.'), 'success')
            return redirect(url_for('settings.index'))

        db.session.commit()
        flash('Settings saved.', 'success')
        return redirect(url_for('settings.index'))

    settings_dict = get_all_settings()
    from flask import current_app
    from app.services import lake
    cfg = current_app.config
    limits = cfg.get('API_RATE_LIMITS') or {}
    security = {'max_failed': cfg.get('MAX_FAILED_LOGINS', 5), 'lockout_minutes': cfg.get('LOCKOUT_MINUTES', 15),
                'api_commands': bool(cfg.get('API_ALLOW_COMMANDS')),
                'api_limits': cfg.get('API_RATE_LIMIT') or ', '.join(f'{r}: {v}' for r, v in limits.items())}
    return render_template('settings/index.html',
                           settings=settings_dict,
                           security=security, lake=lake.status(), retention=lake.retention_policy(),
                           system=system_info(),
                           total_users=User.query.count(),
                           total_nodes=Node.query.filter(Node.is_deleted.isnot(True)).count(),
                           total_logs=ActivityLog.query.count())


def system_info():
    """What this server actually runs (read live, never hardcoded)."""
    import platform
    import flask
    from flask import current_app
    engine = db.engine
    names = {'mysql': 'MariaDB / MySQL', 'sqlite': 'SQLite', 'postgresql': 'PostgreSQL'}
    db_name = names.get(engine.dialect.name, engine.dialect.name)
    try:
        if engine.dialect.name == 'sqlite':
            db_version = db.session.execute(db.text('select sqlite_version()')).scalar()
        else:
            db_version = db.session.execute(db.text('select version()')).scalar()
        if engine.dialect.name == 'mysql' and 'mariadb' in (db_version or '').lower():
            db_name = 'MariaDB'
        elif engine.dialect.name == 'mysql':
            db_name = 'MySQL'
    except Exception:
        db_version = None
    return {'version': current_app.config.get('VERSION', 'dev'),
            'python': platform.python_version(),
            'flask': flask.__version__,
            'database': db_name,
            'database_version': (db_version or '').split('-')[0] or None}
