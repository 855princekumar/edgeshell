"""Sign-in, two-factor authentication, password recovery and self sign-up.

Sign-in order
  1. username + password (lockout and identical errors as before)
  2. second factor: set up an authenticator on first sign-in (then save the
     backup codes), otherwise a code from the app or a backup code
  3. session limit: confirm before another device is signed out
  4. required steps: an administrator's personal email for reset links,
     then a new password if one was issued
Steps 1 and 2 count toward the same lockout. Nothing is signed in until
step 2 has passed.
"""
import hashlib
import re
import time
from datetime import datetime, timedelta

from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError

from app import db, limiter
from app.models.user import PasswordResetRequest, User
from app.services import mfa_service as mfa
from app.services.activity_service import log_activity
from app.services.session_service import (current_session, end_session,
                                          idle_limits, mark_active,
                                          revoke_sessions, session_limit,
                                          sessions_to_replace, start_session)

TAKEOVER_WINDOW_S = 120   # seconds the 'sign out other device?' prompt stays valid
MFA_WINDOW_S = 300        # seconds to finish the second factor after the password
MIN_PASSWORD = 10
USERNAME_RE = re.compile(r'^[A-Za-z0-9._-]{3,64}$')

# Same message for every failure (wrong password, unknown user, locked
# account) so the login form cannot be used to discover valid usernames.
LOGIN_FAILED = 'Invalid username or password, or the account is temporarily locked.'
CODE_FAILED = 'That code is not valid. Check the time on your phone, or use a backup code.'
MFA_DOWN = ('The two-factor verification service is not responding. '
            'Please try again in a minute.')
_DUMMY_HASH = None

auth_bp = Blueprint('auth', __name__)


def _burn_password_check(password):
    """Spend the same bcrypt time when the username does not exist, so response
    timing does not reveal which usernames are real."""
    global _DUMMY_HASH
    from app import bcrypt
    if _DUMMY_HASH is None:
        _DUMMY_HASH = bcrypt.generate_password_hash('edgeshell-timing-pad').decode()
    bcrypt.check_password_hash(_DUMMY_HASH, password)


def _safe_next(next_page):
    if next_page and next_page.startswith('/') and not next_page.startswith('//'):
        return next_page
    return None


def _locked(user):
    return bool(user.locked_until and user.locked_until > datetime.utcnow())


def _record_failure(user, what='password'):
    """Count a failed password or code; lock the account at the limit."""
    user.failed_logins = (user.failed_logins or 0) + 1
    limit = current_app.config.get('MAX_FAILED_LOGINS', 5)
    if user.failed_logins >= limit:
        mins = current_app.config.get('LOCKOUT_MINUTES', 15)
        user.locked_until = datetime.utcnow() + timedelta(minutes=mins)
        user.failed_logins = 0
        log_activity(f'Account locked: {user.username} (failed {what})', status='warning')
    db.session.commit()


# ---------------------------------------------------------------- step 1

@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('20 per minute', methods=['POST'])   # count guesses, not page loads
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        session.pop('mfa', None)
        session.pop('pending_login', None)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()

        if not user:
            _burn_password_check(password)
            flash(LOGIN_FAILED, 'danger')
            return render_template('auth/login.html')

        # Locked: refuse without checking the password, so guesses during the
        # lockout window are worthless. Same message as any other failure.
        if _locked(user):
            _burn_password_check(password)
            flash(LOGIN_FAILED, 'danger')
            return render_template('auth/login.html')

        if user.check_password(password):
            if user.is_pending():
                flash('Your account is waiting for an administrator to approve it.', 'info')
                return render_template('auth/login.html')
            if user.is_active:
                return _password_ok(user, request.args.get('next'))

        _record_failure(user)
        flash(LOGIN_FAILED, 'danger')

    return render_template('auth/login.html')


def _password_ok(user, next_page):
    if not mfa.mfa_required():
        return _second_factor_ok(user, next_page)
    session['mfa'] = {'uid': user.id, 'ts': time.time(), 'next': _safe_next(next_page),
                      'stage': 'verify' if mfa.is_enrolled(user) else 'setup'}
    if session['mfa']['stage'] == 'verify':
        return redirect(url_for('auth.mfa_verify'))
    return redirect(url_for('auth.mfa_setup'))


# ---------------------------------------------------------------- step 2

def _pending_mfa(stage):
    """The user part-way through sign-in, or None if the step expired."""
    p = session.get('mfa')
    if not p or p.get('stage') != stage or time.time() - p.get('ts', 0) > MFA_WINDOW_S:
        return None
    user = db.session.get(User, p.get('uid'))
    if not user or not user.is_active or user.is_pending() or _locked(user):
        return None
    return user


def _expired():
    session.pop('mfa', None)
    flash('That sign-in step expired. Please sign in again.', 'warning')
    return redirect(url_for('auth.login'))


@auth_bp.route('/login/verify', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def mfa_verify():
    user = _pending_mfa('verify')
    if not user:
        return _expired()
    if request.method == 'POST':
        try:
            kind = mfa.verify(user, request.form.get('code', ''))
        except mfa.MFAUnavailable as e:
            current_app.logger.error('2FA check failed: %s', e)
            flash(MFA_DOWN, 'danger')
            return render_template('auth/mfa_verify.html', username=user.username)
        if not kind:
            db.session.rollback()
            _record_failure(user, '2FA code')
            if _locked(user):
                return _expired()
            flash(CODE_FAILED, 'danger')
            return render_template('auth/mfa_verify.html', username=user.username)
        db.session.commit()
        nxt = session.pop('mfa', {}).get('next')
        if kind == 'backup':
            left = mfa.backup_codes_left(user)
            flash(f'Signed in with a backup code. {left if left is not None else "Some"} '
                  'backup code(s) left; create new ones under My account.', 'warning')
        return _second_factor_ok(user, nxt, via=kind)
    return render_template('auth/mfa_verify.html', username=user.username)


@auth_bp.route('/login/setup', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def mfa_setup():
    user = _pending_mfa('setup')
    if not user:
        return _expired()
    try:
        if request.method == 'POST':
            codes = mfa.confirm_enrollment(user, request.form.get('code', ''))
            if codes:
                db.session.commit()
                session['mfa'] = dict(session['mfa'], stage='saved', ts=time.time())
                log_activity(f'Two-factor authentication set up: {user.username}',
                             status='success')
                return render_template('auth/backup_codes.html', codes=codes,
                                       username=user.username, is_admin=user.is_admin(),
                                       generated=datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))
            db.session.rollback()
            _record_failure(user, '2FA setup code')
            if _locked(user):
                return _expired()
            flash('That code did not match. Scan the new QR code below and try again.', 'danger')
        uri, secret = mfa.begin_enrollment(user)
        db.session.commit()
    except mfa.MFAUnavailable as e:
        db.session.rollback()
        current_app.logger.error('2FA enrollment failed: %s', e)
        flash(MFA_DOWN, 'danger')
        return redirect(url_for('auth.login'))
    return render_template('auth/mfa_setup.html', username=user.username,
                           qr=mfa.qr_svg(uri), secret=_group(secret))


@auth_bp.route('/login/setup/done', methods=['POST'])
def mfa_setup_done():
    user = _pending_mfa('saved')
    if not user:
        return _expired()
    nxt = session.pop('mfa', {}).get('next')
    return _second_factor_ok(user, nxt, via='setup')


def _group(secret):
    return ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ---------------------------------------------------------------- step 3

def _second_factor_ok(user, next_page, via=None):
    replace = sessions_to_replace(user)
    if replace:
        # At the session limit: ask before signing a device out.
        session['pending_login'] = {'uid': user.id, 'ts': time.time(),
                                    'next': _safe_next(next_page), 'via': via}
        return render_template('auth/login.html', takeover=replace,
                               takeover_user=user.username,
                               session_limit=session_limit(user))
    return _finish_login(user, next_page, via)


def _finish_login(user, next_page=None, via=None):
    """Password and second factor verified. Create a session and sign in."""
    user.failed_logins = 0
    user.locked_until = None
    user.last_login = datetime.utcnow()
    user.last_login_ip = request.remote_addr

    login_user(user, remember=False)
    session.permanent = True
    replaced = start_session(user)   # signs out the oldest device(s) over the limit
    db.session.commit()

    how = {'totp': ' with 2FA', 'backup': ' with a backup code', 'setup': ' (2FA set up)'}.get(via, '')
    if replaced:
        where = '; '.join(f"{r.get('device_type') or ''} {r.get('agent') or 'another device'}, "
                          f"{r.get('ip') or 'unknown IP'}".strip() for r in replaced)
        log_activity(f'Login{how} (previous session signed out)', response=where,
                     status='warning')
        flash(f'Signed in. Your previous session ({where}) has been signed out.', 'info')
    else:
        log_activity(f'Login{how}', status='success')

    if user.needs_recovery_email():
        return redirect(url_for('account.email'))
    if user.must_change_password:
        flash('Please set a new password before continuing.', 'warning')
        return redirect(url_for('auth.change_password'))
    return redirect(_safe_next(next_page) or url_for('dashboard.index'))


@auth_bp.route('/login/confirm', methods=['POST'])
@limiter.limit('20 per minute')
def login_confirm():
    pending = session.pop('pending_login', None)
    if not pending or time.time() - pending.get('ts', 0) > TAKEOVER_WINDOW_S:
        flash('That sign-in request expired. Please sign in again.', 'warning')
        return redirect(url_for('auth.login'))
    user = db.session.get(User, pending.get('uid'))
    if not user or not user.is_active:
        flash(LOGIN_FAILED, 'danger')
        return redirect(url_for('auth.login'))
    return _finish_login(user, pending.get('next'), pending.get('via'))


@auth_bp.route('/login/cancel')
def login_cancel():
    had_takeover = session.pop('pending_login', None) is not None
    session.pop('mfa', None)
    flash('Sign-in cancelled.' + (' The other session is still active.' if had_takeover else ''),
          'info')
    return redirect(url_for('auth.login'))


# ---------------------------------------------------------------- session pings

@auth_bp.route('/session/status')
@limiter.exempt
def session_status():
    """Polled by every page. The session hook answers 401 first if this
    browser was signed out; otherwise report idle time for the prompt."""
    if not current_user.is_authenticated:
        return jsonify({'authenticated': False})
    idle, warn = idle_limits()
    row = current_session()
    last = (row.active_at or row.started_at) if row else datetime.utcnow()
    return jsonify({'authenticated': True,
                    'idle_s': int((datetime.utcnow() - last).total_seconds()),
                    'idle_after_s': idle, 'warn_s': warn})


@auth_bp.route('/session/activity', methods=['POST'])
@limiter.exempt
@login_required
def session_activity():
    """The page saw a click or key press, or the person answered the
    'still there?' prompt."""
    row = current_session()
    if row:
        mark_active(row)
        db.session.commit()
    return jsonify({'ok': True})


@auth_bp.route('/session/expire', methods=['POST'])
@login_required
def session_expire():
    """The 'still there?' countdown ran out in this browser."""
    log_activity('Signed out (inactive)', status='info')
    row = current_session()
    if row:
        row.revoked_at = datetime.utcnow()
        row.revoked_reason = 'idle'
        db.session.commit()
    logout_user()
    session.clear()
    return jsonify({'ok': True, 'login_url': url_for('auth.login')})


# ---------------------------------------------------------------- passwords

def _password_problem(new_pw, confirm_pw, user=None):
    if len(new_pw) < MIN_PASSWORD:
        return f'Passwords must be at least {MIN_PASSWORD} characters.'
    if new_pw != confirm_pw:
        return 'Passwords do not match.'
    if user is not None and user.check_password(new_pw):
        return 'Choose a password you have not used for this account.'
    return None


@auth_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')

        if not current_user.check_password(current_pw):
            flash('Current password is incorrect.', 'danger')
        elif (problem := _password_problem(new_pw, confirm_pw, current_user)):
            flash(problem, 'danger')
        else:
            current_user.set_password(new_pw)
            current_user.must_change_password = False
            # Anyone else holding this account's session is signed out.
            others = revoke_sessions(current_user, 'password_changed', keep_current=True)
            db.session.commit()
            log_activity('Password changed', status='success')
            flash('Password changed successfully.'
                  + (f' {others} other session(s) were signed out.' if others else ''),
                  'success')
            return redirect(url_for('dashboard.index'))

    return render_template('auth/change_password.html', min_password=MIN_PASSWORD)


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='edgeshell-reset')


def _pw_fingerprint(user):
    """Changes whenever the password does, so a reset link works only once."""
    return hashlib.sha256((user.password_hash or '').encode()).hexdigest()[:20]


def reset_link(user):
    base = current_app.config.get('PUBLIC_URL', '')
    token = _reset_serializer().dumps({'u': user.id, 'p': _pw_fingerprint(user)})
    return f"{base}{url_for('auth.reset_with_link', token=token)}"


FORGOT_DONE = ('If that account exists, a reset link was sent to its verified email address. '
               'Accounts without one get a reset from an administrator, who has been told.')


@auth_bp.route('/forgot', methods=['GET', 'POST'])
@limiter.limit('5 per hour', methods=['POST'])
def forgot():
    from app.services.email_service import account_mail_available, send_account_email
    mail_ready = bool(account_mail_available() and current_app.config.get('PUBLIC_URL'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        user = User.query.filter_by(username=username).first()
        if user and user.is_active and not user.is_pending():
            # Every role: a link to the verified address. 2FA stays on, so the
            # mailbox alone is never enough to get in.
            sent = False
            if mail_ready and user.email_verified():
                minutes = current_app.config.get('RESET_LINK_MINUTES', 30)
                ok, detail = send_account_email(
                    user.email, 'EdgeShell password reset',
                    f'A password reset was requested for the EdgeShell account '
                    f'"{user.username}" from {request.remote_addr}.\n\n'
                    f'Open this link within {minutes} minutes to choose a new password:\n'
                    f'{reset_link(user)}\n\n'
                    'Two-factor authentication stays on. If you did not ask for this, '
                    'ignore this email; your password has not changed.\n')
                log_activity(f'Password reset link requested: {user.username}',
                             response=detail, status='success' if ok else 'failure')
                sent = ok
            elif user.is_admin():
                log_activity(f'Password reset link requested: {user.username}',
                             response='mail not configured or email not verified', status='warning')
            if not sent and not user.is_admin() and \
                    not PasswordResetRequest.query.filter_by(user_id=user.id, status='open').first():
                db.session.add(PasswordResetRequest(user_id=user.id,
                                                    ip=(request.remote_addr or '')[:45]))
                db.session.commit()
                log_activity(f'Password reset requested: {user.username}', status='info')
        else:
            _burn_password_check(username)
        flash(FORGOT_DONE, 'info')
        return redirect(url_for('auth.login'))
    return render_template('auth/forgot.html', mail_ready=mail_ready)


@auth_bp.route('/reset/<token>', methods=['GET', 'POST'])
@limiter.limit('10 per hour', methods=['POST'])
def reset_with_link(token):
    minutes = current_app.config.get('RESET_LINK_MINUTES', 30)
    try:
        data = _reset_serializer().loads(token, max_age=minutes * 60)
    except (SignatureExpired, BadSignature):
        data = None
    user = db.session.get(User, data.get('u')) if isinstance(data, dict) else None
    if not user or not user.is_active or not user.email_verified() \
            or data.get('p') != _pw_fingerprint(user):
        flash('That reset link is invalid, already used or expired. Request a new one.', 'danger')
        return redirect(url_for('auth.forgot'))
    if request.method == 'POST':
        problem = _password_problem(request.form.get('new_password', ''),
                                    request.form.get('confirm_password', ''), user)
        if problem:
            flash(problem, 'danger')
        else:
            _apply_self_reset(user, request.form['new_password'], 'email link')
            flash('Password changed. Sign in with the new password and a 2FA code.', 'success')
            return redirect(url_for('auth.login'))
    return render_template('auth/reset.html', username=user.username)


@auth_bp.route('/recover', methods=['GET', 'POST'])
@limiter.limit('10 per hour', methods=['POST'])
def recover():
    """Administrator forgot the password: username + one backup code."""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        code = request.form.get('code', '')
        new_pw = request.form.get('new_password', '')
        problem = _password_problem(new_pw, request.form.get('confirm_password', ''))
        if problem:
            flash(problem, 'danger')
            return render_template('auth/recover.html')
        user = User.query.filter_by(username=username).first()
        failed = 'That username and backup code do not match, or the account is locked.'
        if not user or not user.is_admin() or not user.is_active or _locked(user):
            _burn_password_check(code)
            flash(failed, 'danger')
            return render_template('auth/recover.html')
        normalized = mfa.normalize_code(code)
        if len(normalized) == mfa.TOTP_DIGITS and normalized.isdigit():
            flash('Use a backup code here, not a code from the authenticator app.', 'danger')
            return render_template('auth/recover.html')
        try:
            kind = mfa.verify(user, normalized)
        except mfa.MFAUnavailable:
            flash(MFA_DOWN, 'danger')
            return render_template('auth/recover.html')
        if kind != 'backup':
            db.session.rollback()
            _record_failure(user, 'backup code')
            flash(failed, 'danger')
            return render_template('auth/recover.html')
        _apply_self_reset(user, new_pw, 'backup code')
        flash('Password changed with a backup code. Sign in with the new password and '
              'a 2FA code (or another backup code).', 'success')
        return redirect(url_for('auth.login'))
    return render_template('auth/recover.html')


def _apply_self_reset(user, new_password, how):
    user.set_password(new_password)
    user.must_change_password = False
    user.failed_logins = 0
    user.locked_until = None
    revoke_sessions(user, 'password_changed')
    db.session.commit()
    log_activity(f'Password reset by {how}: {user.username}', status='warning')
    _notify_admin_reset(user, how)


def _notify_admin_reset(user, how):
    from app.services.email_service import account_mail_available, send_account_email
    if account_mail_available() and user.email_verified():
        send_account_email(user.email, 'EdgeShell password changed',
                           f'The password for EdgeShell account "{user.username}" was '
                           f'changed using a {how} from {request.remote_addr}. If this was not '
                           'you, tell an administrator now.\n')


# ---------------------------------------------------------------- sign-up

@auth_bp.route('/signup', methods=['GET', 'POST'])
@limiter.limit('5 per hour', methods=['POST'])
def signup():
    if not current_app.config.get('SIGNUP_ENABLED', True):
        flash('Self sign-up is turned off. Ask an administrator for an account.', 'info')
        return redirect(url_for('auth.login'))
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    form = {'username': request.form.get('username', '').strip(),
            'email': request.form.get('email', '').strip().lower()}
    if request.method == 'POST':
        password = request.form.get('password', '')
        problem = _password_problem(password, request.form.get('confirm_password', ''))
        if not USERNAME_RE.match(form['username']):
            flash('Usernames are 3 to 64 letters, digits, dots, dashes or underscores.', 'danger')
        elif len(form['email']) > 120 or not re.match(r'^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$', form['email']):
            flash('Enter a valid email address.', 'danger')
        elif problem:
            flash(problem, 'danger')
        elif User.query.filter(db.or_(db.func.lower(User.username) == form['username'].lower(),
                                      db.func.lower(User.email) == form['email'])).first():
            flash('That username or email cannot be used. Choose another, or sign in.', 'danger')
        else:
            user = User(username=form['username'], email=form['email'], role='monitor',
                        is_active=False, approval_status='pending',
                        must_change_password=False, max_sessions=1)
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash('That username or email cannot be used. Choose another, or sign in.',
                      'danger')
                return render_template('auth/signup.html', form=form)
            log_activity(f'Sign-up request: {user.username}', status='info')
            flash('Request sent. An administrator will approve it and choose your role; '
                  'then sign in and set up two-factor authentication.', 'success')
            return redirect(url_for('auth.login'))
    return render_template('auth/signup.html', form=form)


@auth_bp.route('/logout')
@login_required
def logout():
    log_activity('Logout', status='success')
    end_session(current_user)
    db.session.commit()
    logout_user()
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))
