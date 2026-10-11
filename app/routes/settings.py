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
            for key in ['max_login_attempts', 'login_rate_limit', 'blocked_commands']:
                val = request.form.get(key)
                if val is not None:
                    save_setting(key, val)
            save_setting('require_sudo_confirm', 'true' if request.form.get('require_sudo_confirm') else 'false')
            save_setting('audit_all_commands', 'true' if request.form.get('audit_all_commands') else 'false')

        elif section == 'ansible':
            for key in ['ansible_playbooks_dir', 'ansible_inventory_dir',
                        'default_remote_user', 'upload_staging_dir']:
                val = request.form.get(key)
                if val is not None:
                    save_setting(key, val)
            save_setting('ansible_enabled', 'true' if request.form.get('ansible_enabled') else 'false')

        elif section == 'clear_logs':
            ActivityLog.query.delete()
            flash('Activity logs cleared.', 'success')
            db.session.commit()
            return redirect(url_for('settings.index'))

        elif section == 'clear_node_logs':
            from app.models import NodeLog
            NodeLog.query.delete()
            flash('Node logs cleared.', 'success')
            db.session.commit()
            return redirect(url_for('settings.index'))

        db.session.commit()
        flash('Settings saved.', 'success')
        return redirect(url_for('settings.index'))

    settings_dict = get_all_settings()
    return render_template('settings/index.html',
                           settings=settings_dict,
                           total_users=User.query.count(),
                           total_nodes=Node.query.count(),
                           total_logs=ActivityLog.query.count())
