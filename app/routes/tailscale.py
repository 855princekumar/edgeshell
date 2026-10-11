from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from app.services.tailscale_service import get_tailscale_status, connect_tailscale, disconnect_tailscale
from app import db
from app.models import Settings
from app.services.activity_service import log_activity
from functools import wraps

tailscale_bp = Blueprint('tailscale', __name__, url_prefix='/tailscale')


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash('Administrator access required.', 'danger')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


def get_settings_dict():
    rows = Settings.query.all()
    return {r.key: r.value for r in rows}


def save_setting(key, value):
    row = Settings.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        db.session.add(Settings(key=key, value=str(value)))


@tailscale_bp.route('/', methods=['GET', 'POST'])
@login_required
@admin_required
def index():
    if request.method == 'POST':
        node_name = request.form.get('node_name', '')
        auth_key = request.form.get('auth_key', '')
        save_setting('tailscale_node_name', node_name)
        if auth_key:
            save_setting('tailscale_auth_key', auth_key)
        db.session.commit()
        flash('Tailscale configuration saved.', 'success')
        return redirect(url_for('tailscale.index'))

    settings = get_settings_dict()
    status = get_tailscale_status()
    return render_template('settings/tailscale.html', settings=settings, status=status)


@tailscale_bp.route('/status')
@login_required
def status():
    return jsonify(get_tailscale_status())


@tailscale_bp.route('/connect', methods=['POST'])
@login_required
@admin_required
def connect():
    settings = get_settings_dict()
    auth_key = settings.get('tailscale_auth_key', '')
    if not auth_key:
        return jsonify({'success': False, 'message': 'No Tailscale auth key configured. Add one in Settings.'})
    success, msg = connect_tailscale(auth_key)
    log_activity('Tailscale Connect', status='success' if success else 'failure', response=msg)
    return jsonify({'success': success, 'message': msg})


@tailscale_bp.route('/disconnect', methods=['POST'])
@login_required
@admin_required
def disconnect():
    success, msg = disconnect_tailscale()
    log_activity('Tailscale Disconnect', status='success' if success else 'failure', response=msg)
    return jsonify({'success': success, 'message': msg})
