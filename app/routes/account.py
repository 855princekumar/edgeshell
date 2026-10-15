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

# Unambiguous (dots only between domain labels) so matching stays linear;
# input is also capped at the column size before it is matched.
EMAIL_RE = r'^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$'


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


@account_bp.route('/api/guide')
@login_required
def api_guide():
    """The API guide (docs/API.md) as one readable page, opened in a new tab.
    Built by tools/build_api_guide.py; CI checks it matches the doc."""
    return render_template('account/api_guide.html')


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


def _verify_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='edgeshell-email-verify')


VERIFY_HOURS = 24


def _send_verification(user, address):
    """Email a one-use link that proves `address` reaches this user."""
    from app.services.email_service import send_account_email
    token = _verify_serializer().dumps({'u': user.id, 'e': address})
    link = f"{current_app.config.get('PUBLIC_URL', '').rstrip('/')}{url_for('account.email_verify', token=token)}"
    return send_account_email(
        address, 'EdgeShell: confirm your email address',
        f'Someone (hopefully you) set this address for the EdgeShell account "{user.username}".\n\n'
        f'Open this link within {VERIFY_HOURS} hours to confirm it:\n{link}\n\n'
        'Password reset links for the account will be sent here. If this was not you, ignore '
        'this email; nothing changes until the link is opened.\n')


@account_bp.route('/email', methods=['GET', 'POST'])
@login_required
@limiter.limit('10 per hour', methods=['POST'])
def email():
    """Set the account email. With outgoing mail set up, a new address is only
    used after the link sent to it is opened (it waits in email_pending)."""
    import re
    from app.services.email_service import mail_ready
    ready = mail_ready()
    required = current_user.needs_recovery_email()
    if request.method == 'POST':
        new = request.form.get('email', '').strip().lower()
        if len(new) > 120 or not re.match(EMAIL_RE, new) or new.endswith('.local') or new.endswith('@example.com'):
            flash('Enter a real email address you can read.', 'danger')
        elif not current_user.check_password(request.form.get('password', '')):
            flash('Your password was not correct.', 'danger')
        elif User.query.filter(db.or_(db.func.lower(User.email) == new, db.func.lower(User.email_pending) == new),
                               User.id != current_user.id).first():
            flash('That email address is used by another account.', 'danger')
        elif ready:
            if new == (current_user.email or '').lower() and current_user.email_verified():
                flash('That address is already verified.', 'info')
                return redirect(url_for('account.email'))
            current_user.email_pending = new
            db.session.commit()
            ok, detail = _send_verification(current_user, new)
            log_activity('Email verification sent', response=new if ok else f'{new}: {detail}',
                         status='success' if ok else 'failure')
            if ok:
                flash(f'We sent a link to {new}. Open it to confirm the address; until then your account '
                      'keeps its current email.', 'info')
            else:
                flash('The verification email could not be sent. Check the address or try again shortly; '
                      'an administrator can check the mail settings.', 'danger')
            return redirect(url_for('account.email'))
        else:
            current_user.email = new
            db.session.commit()
            log_activity('Account email changed', status='success')
            flash('Email saved. It is verified once outgoing mail is set up on this server.', 'success')
            if current_user.must_change_password:
                return redirect(url_for('auth.change_password'))
            return redirect(url_for('account.index') if not required
                            else url_for('dashboard.index'))
    return render_template('account/email.html', required=required, mail_ready=ready,
                           verify_hours=VERIFY_HOURS)


@account_bp.route('/email/resend', methods=['POST'])
@login_required
@limiter.limit('5 per hour')
def email_resend():
    """Send the link again: to the waiting address, or to the current one when
    it has never been verified (accounts from before verification existed)."""
    from app.services.email_service import mail_ready
    target = current_user.email_pending or (current_user.email if current_user.has_real_email()
                                            and not current_user.email_verified() else None)
    if not mail_ready() or not target:
        flash('There is nothing to verify.', 'info')
        return redirect(url_for('account.email'))
    if not current_user.email_pending:
        current_user.email_pending = target
        db.session.commit()
    ok, detail = _send_verification(current_user, target)
    log_activity('Email verification sent', response=target if ok else f'{target}: {detail}',
                 status='success' if ok else 'failure')
    flash(f'Link sent to {target}.' if ok else 'The verification email could not be sent. Try again shortly.',
          'info' if ok else 'danger')
    return redirect(url_for('account.email'))


@account_bp.route('/email/verify/<token>')
@limiter.limit('20 per hour')
def email_verify(token):
    """Opening the link proves the address works. No sign-in needed (the link
    may be opened on a phone); it only confirms the address this account asked
    for, and stops working once another address is requested."""
    from itsdangerous import BadSignature, SignatureExpired
    try:
        data = _verify_serializer().loads(token, max_age=VERIFY_HOURS * 3600)
    except (SignatureExpired, BadSignature):
        data = None
    user = db.session.get(User, data.get('u')) if isinstance(data, dict) else None
    address = data.get('e') if isinstance(data, dict) else None
    if not user or not user.is_active or not address or (user.email_pending or '').lower() != address.lower():
        flash('That confirmation link is invalid, expired or already used. Request a new one.', 'danger')
        return redirect(url_for('account.email') if current_user.is_authenticated else url_for('auth.login'))
    if User.query.filter(db.func.lower(User.email) == address.lower(), User.id != user.id).first():
        flash('That email address is now used by another account.', 'danger')
        return redirect(url_for('auth.login'))
    old = user.email
    user.email, user.email_pending, user.email_verified_at = address, None, datetime.utcnow()
    db.session.commit()
    log_activity(f'Email verified: {user.username}', response=address if old == address else f'{old} -> {address}',
                 status='success')
    flash('Email address confirmed. Password reset links will be sent there.', 'success')
    if current_user.is_authenticated and current_user.id == user.id:
        if user.must_change_password:
            return redirect(url_for('auth.change_password'))
        return redirect(url_for('dashboard.index'))
    return redirect(url_for('auth.login'))


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
