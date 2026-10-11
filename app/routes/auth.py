import time
from datetime import datetime, timedelta
from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, session, current_app, jsonify)
from flask_login import (login_user, logout_user, login_required, current_user)
from app import db, limiter
from app.models.user import User
from app.services.activity_service import log_activity
from app.services.session_service import (sessions_to_replace, session_limit,
                                          start_session, end_session,
                                          revoke_sessions)

TAKEOVER_WINDOW_S = 120  # seconds the 'sign out other device?' prompt stays valid

# Same message for every failure (wrong password, unknown user, locked
# account) so the login form cannot be used to discover valid usernames.
LOGIN_FAILED = 'Invalid username or password, or the account is temporarily locked.'
_DUMMY_HASH = None


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


def _finish_login(user, next_page=None):
    """Password already verified. Create a fresh session token and sign in."""
    user.failed_logins = 0
    user.locked_until = None
    user.last_login = datetime.utcnow()
    user.last_login_ip = request.remote_addr

    login_user(user, remember=False)
    session.permanent = True
    replaced = start_session(user)   # signs out the oldest device(s) over the limit
    db.session.commit()

    if replaced:
        where = '; '.join(f"{r.get('agent') or 'another device'}, {r.get('ip') or 'unknown IP'}"
                          for r in replaced)
        log_activity('Login (previous session signed out)', response=where,
                     status='warning')
        flash(f'Signed in. Your previous session ({where}) has been signed out.',
              'info')

    if user.must_change_password:
        flash('Please set a new password before continuing.', 'warning')
        return redirect(url_for('auth.change_password'))

    if not replaced:
        log_activity('Login', status='success')
    return redirect(_safe_next(next_page) or url_for('dashboard.index'))

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('20 per minute', methods=['POST'])   # count guesses, not page loads
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()

        if not user:
            _burn_password_check(password)
            flash(LOGIN_FAILED, 'danger')
            return render_template('auth/login.html')

        # Locked: refuse without checking the password, so guesses during the
        # lockout window are worthless. Same message as any other failure.
        if user.locked_until and user.locked_until > datetime.utcnow():
            _burn_password_check(password)
            flash(LOGIN_FAILED, 'danger')
            return render_template('auth/login.html')

        if user.is_active and user.check_password(password):
            next_page = request.args.get('next')
            replace = sessions_to_replace(user)
            if replace:
                # At the session limit: ask before signing a device out.
                session['pending_login'] = {'uid': user.id, 'ts': time.time(),
                                            'next': _safe_next(next_page)}
                return render_template('auth/login.html', takeover=replace,
                                       takeover_user=user.username,
                                       session_limit=session_limit(user))
            return _finish_login(user, next_page)

        # Failure path: increment and maybe lock
        user.failed_logins = (user.failed_logins or 0) + 1
        limit = current_app.config.get('MAX_FAILED_LOGINS', 5)
        if user.failed_logins >= limit:
            mins = current_app.config.get('LOCKOUT_MINUTES', 15)
            user.locked_until = datetime.utcnow() + timedelta(minutes=mins)
            user.failed_logins = 0
            log_activity(f'Account locked: {user.username}', status='warning')
        db.session.commit()
        flash(LOGIN_FAILED, 'danger')

    return render_template('auth/login.html')


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
    return _finish_login(user, pending.get('next'))


@auth_bp.route('/login/cancel')
def login_cancel():
    session.pop('pending_login', None)
    flash('Sign-in cancelled. The other session is still active.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/session/status')
@limiter.exempt
def session_status():
    """Polled by every page. The single-session hook answers 401 first if this
    browser was replaced; otherwise report whether we are still signed in."""
    return jsonify({'authenticated': bool(current_user.is_authenticated)})


@auth_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')

        if not current_user.check_password(current_pw):
            flash('Current password is incorrect.', 'danger')
        elif len(new_pw) < 8:
            flash('New password must be at least 8 characters.', 'danger')
        elif new_pw != confirm_pw:
            flash('Passwords do not match.', 'danger')
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

    return render_template('auth/change_password.html')


@auth_bp.route('/logout')
@login_required
def logout():
    log_activity('Logout', status='success')
    end_session(current_user)
    db.session.commit()
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))
