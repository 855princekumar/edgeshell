"""My account: two-factor status and backup codes, signed-in devices,
personal email, and stepping down from the administrator role."""
from datetime import datetime

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)
from flask_login import current_user, login_required, logout_user

from app import db, limiter
from app.models.user import User
from app.services import account_service as accounts
from app.services import mfa_service as mfa
from app.services.activity_service import log_activity
from app.services.session_service import (close_session, device_label,
                                          live_sessions, revoke_sessions)

account_bp = Blueprint('account', __name__, url_prefix='/account')

EMAIL_RE = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'


def _confirm_identity(require_code=True):
    """Password (and a 2FA code when 2FA is on) for sensitive changes."""
    if not current_user.check_password(request.form.get('password', '')):
        return 'Your password was not correct.'
    if require_code and mfa.mfa_required():
        try:
            kind = mfa.verify(current_user, request.form.get('code', ''))
        except mfa.MFAUnavailable:
            return 'The two-factor verification service is not responding. Try again shortly.'
        if not kind:
            db.session.rollback()
            return 'The two-factor code was not valid.'
    return None


@account_bp.route('/')
@login_required
def index():
    sid = session.get('sid')
    devices = live_sessions(current_user)
    devices.sort(key=lambda s: s.active_at or s.started_at, reverse=True)
    left = None
    enrolled = mfa.is_enrolled(current_user)
    if enrolled:
        left = mfa.backup_codes_left(current_user)
    return render_template('account/index.html', devices=devices, current_sid=sid,
                           device_label=device_label, enrolled=enrolled,
                           mfa_on=mfa.mfa_required(), backup_left=left,
                           provider=mfa.provider_name(),
                           admins=accounts.admins(), max_admins=accounts.MAX_ADMINS,
                           now=datetime.utcnow())


@account_bp.route('/api')
@login_required
def api():
    """API: your own sessions and service keys, what you may do, how to start.
    In the sidebar for every role (Intelligence, API)."""
    from app.models.api import ApiToken
    from app.services import api_auth
    api_rows = (ApiToken.query.filter(ApiToken.user_id == current_user.id, ApiToken.kind.in_(('session', 'key')),
                                      ApiToken.revoked_at.is_(None), ApiToken.expires_at > datetime.utcnow())
                .order_by(ApiToken.kind, ApiToken.created_at.desc()).all())
    p = api_auth.policy()
    mine = sorted(api_auth.effective_scopes(current_user), key=api_auth.SCOPES.index)
    blocked = getattr(current_user, 'api_access', True) is False
    return render_template('account/api.html', api_rows=api_rows, api_scopes=mine,
                           usable=[s for s in mine if s in p['scopes']], policy=p, blocked=blocked,
                           base_url=(current_app.config.get('PUBLIC_URL') or request.host_url).rstrip('/') + '/api/v1',
                           new_key=session.pop('new_api_key', None))


@account_bp.route('/api-keys', methods=['POST'])
@login_required
@limiter.limit('10 per hour', methods=['POST'])
def create_api_key():
    """Create a service key from the web app: password and a 2FA code
    again, so an unattended signed-in browser cannot be used to mint keys."""
    from datetime import timedelta
    from app.models.api import ApiToken
    from app.routes.auth import _record_failure
    from app.services import api_auth
    f = request.form
    name = (f.get('name') or '').strip()[:80]
    purpose = (f.get('purpose') or '').strip()[:200]
    scopes = [s for s in f.getlist('scopes') if s in api_auth.SCOPES]
    blocked = api_auth.refusal(current_user)
    if blocked or not api_auth.policy()['keys']:
        flash('Creating service keys is turned off for this account or this server by an administrator.', 'danger')
        return redirect(url_for('account.api'))
    try:
        days = max(1, min(365, int(f.get('expires_in_days') or 90)))
    except ValueError:
        days = 90
    if not name or '<' in name or '>' in name or not scopes or len(purpose) < 5 or '<' in purpose or '>' in purpose:
        flash('Give the key a name, a purpose (what it is used for) and at least one permission.', 'danger')
        return redirect(url_for('account.api'))
    if not current_user.check_password(f.get('password') or ''):
        _record_failure(current_user, 'password (API key creation)')
        flash('Password or code is not valid.', 'danger')
        return redirect(url_for('account.api'))
    try:
        if mfa.mfa_required() and not mfa.verify(current_user, f.get('code') or ''):
            _record_failure(current_user, '2FA code (API key creation)')
            flash('Password or code is not valid.', 'danger')
            return redirect(url_for('account.api'))
    except mfa.MFAUnavailable:
        flash('The two-factor service is not responding. Try again shortly.', 'danger')
        return redirect(url_for('account.api'))
    if not set(scopes) <= api_auth.effective_scopes(current_user):
        flash('A key cannot have more permission than your role.', 'danger')
        return redirect(url_for('account.api'))
    ips, err = api_auth.parse_ip_list(f.get('allowed_ips'))
    if err:
        flash(f'Allowed addresses: {err}.', 'danger')
        return redirect(url_for('account.api'))
    live = ApiToken.query.filter(ApiToken.user_id == current_user.id, ApiToken.kind == 'key',
                                 ApiToken.revoked_at.is_(None), ApiToken.expires_at > datetime.utcnow()).count()
    if live >= api_auth.MAX_KEYS_PER_USER:
        flash(f'At most {api_auth.MAX_KEYS_PER_USER} active keys. Revoke one first.', 'danger')
        return redirect(url_for('account.api'))
    row, plaintext = api_auth.issue(current_user, 'key', scopes=scopes, name=name,
                                    expires_at=datetime.utcnow() + timedelta(days=days), allowed_ips=ips)
    row.purpose = purpose
    db.session.commit()
    log_activity('API key created', response=f'{row.prefix} "{name}" for "{purpose}", scopes {row.scopes}, {days} days'
                 + (f', from {ips}' if ips else ''), status='success')
    session['new_api_key'] = {'key': plaintext, 'name': name}   # shown once, then dropped
    return redirect(url_for('account.api'))


@account_bp.route('/api-keys/<int:token_id>/revoke', methods=['POST'])
@login_required
def revoke_api_credential(token_id):
    from app.models.api import ApiToken
    from app.services import api_auth
    row = ApiToken.query.filter(ApiToken.id == token_id, ApiToken.user_id == current_user.id,
                                ApiToken.kind.in_(('session', 'key'))).first()
    if row:
        api_auth.revoke(row, 'revoked')
        db.session.commit()
        log_activity('API credential revoked', response=f'{row.kind} {row.prefix}' + (f' "{row.name}"' if row.name else ''),
                     status='success')
        flash('Revoked. It stops working immediately.', 'success')
    return redirect(url_for('account.api'))


@account_bp.route('/prefs', methods=['POST'])
@login_required
def save_prefs():
    """Remember page settings for this account, e.g. {"monitoring_auto_refresh": false}."""
    from flask import jsonify
    data = request.get_json(silent=True) or {}
    if not data or any(k not in User.PREFS for k in data):
        return jsonify({'success': False, 'message': 'Unknown setting.'}), 400
    try:
        for k, v in data.items():
            current_user.set_pref(k, v)
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid value.'}), 400
    db.session.commit()
    return jsonify({'success': True})


@account_bp.route('/sessions/<int:session_id>/close', methods=['POST'])
@login_required
def close(session_id):
    row = close_session(current_user, session_id)
    if not row:
        flash('That session has already ended.', 'info')
        return redirect(url_for('account.index'))
    db.session.commit()
    log_activity('Closed own session', response=device_label(row), status='success')
    if row.token == session.get('sid'):
        logout_user()
        session.clear()
        flash('This session was closed.', 'info')
        return redirect(url_for('auth.login'))
    flash(f'Signed out {device_label(row)}.', 'success')
    return redirect(url_for('account.index'))


@account_bp.route('/backup-codes', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def backup_codes():
    problem = _confirm_identity()
    if problem:
        flash(problem, 'danger')
        return redirect(url_for('account.index'))
    try:
        codes = mfa.regenerate_backup_codes(current_user)
    except mfa.MFAUnavailable:
        db.session.rollback()
        flash('The two-factor verification service is not responding. Try again shortly.',
              'danger')
        return redirect(url_for('account.index'))
    if not codes:
        flash('Set up two-factor authentication first.', 'warning')
        return redirect(url_for('account.index'))
    db.session.commit()
    log_activity('Backup codes regenerated', status='warning')
    return render_template('account/backup_codes.html', codes=codes,
                           username=current_user.username, is_admin=current_user.is_admin(),
                           generated=datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))


@account_bp.route('/2fa/reset', methods=['POST'])
@login_required
@limiter.limit('5 per hour')
def reset_2fa():
    """New phone: drop the current authenticator; set up again at next sign-in."""
    problem = _confirm_identity()
    if problem:
        flash(problem, 'danger')
        return redirect(url_for('account.index'))
    try:
        mfa.remove(current_user)
    except mfa.MFAUnavailable:
        db.session.rollback()
        flash('The two-factor verification service is not responding. Try again shortly.',
              'danger')
        return redirect(url_for('account.index'))
    revoke_sessions(current_user, 'mfa_reset')
    db.session.commit()
    log_activity('Two-factor authentication reset by user', status='warning')
    logout_user()
    session.clear()
    flash('Two-factor authentication was removed. Sign in again to set up your new device.',
          'info')
    return redirect(url_for('auth.login'))


@account_bp.route('/email', methods=['GET', 'POST'])
@login_required
def email():
    import re
    required = current_user.needs_recovery_email()
    if request.method == 'POST':
        new = request.form.get('email', '').strip().lower()
        if not re.match(EMAIL_RE, new) or new.endswith('.local'):
            flash('Enter a real email address you can read.', 'danger')
        elif not current_user.check_password(request.form.get('password', '')):
            flash('Your password was not correct.', 'danger')
        elif User.query.filter(db.func.lower(User.email) == new,
                               User.id != current_user.id).first():
            flash('That email address is used by another account.', 'danger')
        else:
            current_user.email = new
            db.session.commit()
            log_activity('Account email changed', status='success')
            flash('Email saved.' + (' Password reset links will be sent there.'
                                     if current_user.is_admin() else ''), 'success')
            if current_user.must_change_password:
                return redirect(url_for('auth.change_password'))
            return redirect(url_for('account.index') if not required
                            else url_for('dashboard.index'))
    from app.services.email_service import account_mail_available
    return render_template('account/email.html', required=required,
                           mail_ready=bool(account_mail_available()
                                           and current_app.config.get('PUBLIC_URL')))


@account_bp.route('/step-down', methods=['POST'])
@login_required
@limiter.limit('5 per hour')
def step_down():
    if not current_user.is_admin():
        flash('You are not an administrator.', 'warning')
        return redirect(url_for('account.index'))
    if request.form.get('confirm') != 'STEP DOWN':
        flash('Type STEP DOWN to confirm.', 'danger')
        return redirect(url_for('account.index'))
    problem = _confirm_identity()
    if problem:
        flash(problem, 'danger')
        return redirect(url_for('account.index'))
    was_owner = current_user.is_owner
    ok, message, successor = accounts.step_down(current_user, request.form.get('new_role', ''))
    if not ok:
        db.session.rollback()
        flash(message, 'danger')
        return redirect(url_for('account.index'))
    db.session.commit()
    log_activity(f'Stepped down from administrator to {current_user.role}'
                 + (f'; ownership passed to {successor.username}' if successor else ''),
                 status='warning')
    flash(f'You are now {current_user.role}.'
          + (f' {successor.username} is the new owner.' if was_owner and successor else ''),
          'success')
    return redirect(url_for('dashboard.index'))
