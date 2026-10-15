"""The gate in front of SenseHive (/sensehive/, ADR 0013): administrators only,
and only with a fresh second factor.

- From the admin panel: the sidebar button POSTs (CSRF token) to
  /tools/sensehive/launch, which issues a SenseHive pass and opens SenseHive.
- Typed or bookmarked /sensehive/: nginx asks /internal/admin-check; without a
  pass the browser is sent to /tools/sensehive/unlock, which asks for a 2FA code
  (sign-in first when needed). Wrong codes count toward the account lockout.

The pass is a signed cookie, HttpOnly, SameSite=Strict, Path=/sensehive/,
bound to this browser's EdgeShell session: it is useless without that session,
dies with it (sign-out, revocation, idle timeout) and expires after
PASS_HOURS. nginx never forwards EdgeShell's cookies to SenseHive.
"""
import hashlib

from flask import (Blueprint, current_app, flash, redirect, render_template, request,
                   session, url_for)
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import limiter
from app.services.activity_service import log_activity

sensehive_bp = Blueprint('sensehive', __name__, url_prefix='/tools/sensehive')

COOKIE = 'es_sensehive'
PATH = '/sensehive/'
PASS_HOURS = 4


def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='edgeshell-sensehive-pass')


def _sid_tag():
    sid = session.get('sid') or ''
    return hashlib.sha256(sid.encode()).hexdigest()[:24] if sid else ''


def _ready_admin():
    """A signed-in administrator who has finished every sign-in step."""
    return (current_user.is_authenticated and current_user.is_admin()
            and not current_user.needs_recovery_email() and not current_user.must_change_password)


def pass_valid():
    raw = request.cookies.get(COOKIE)
    if not raw or not _ready_admin() or not _sid_tag():
        return False
    try:
        data = _serializer().loads(raw, max_age=PASS_HOURS * 3600)
    except (BadSignature, SignatureExpired):
        return False
    return data.get('u') == current_user.id and data.get('s') == _sid_tag()


def _grant(resp, how):
    token = _serializer().dumps({'u': current_user.id, 's': _sid_tag()})
    resp.set_cookie(COOKIE, token, max_age=PASS_HOURS * 3600, path=PATH, httponly=True, samesite='Strict',
                    secure=bool(current_app.config.get('SESSION_COOKIE_SECURE')))
    log_activity('SenseHive opened', response=how, status='success')
    return resp


@sensehive_bp.route('/launch', methods=['POST'])
@login_required
@limiter.limit('30 per hour')
def launch():
    """From the admin panel only: a POST with the page's CSRF token."""
    if not _ready_admin():
        return redirect(url_for('dashboard.index'))
    return _grant(redirect(PATH), 'from the admin panel')


@sensehive_bp.route('/unlock', methods=['GET', 'POST'])
@login_required
@limiter.limit('10 per hour', methods=['POST'])
def unlock():
    """Direct visits: a fresh second factor before SenseHive opens."""
    from app.routes.auth import _locked, _record_failure
    from app.services import mfa_service as mfa
    if not _ready_admin():
        flash('SenseHive is for administrators only.', 'danger')
        return redirect(url_for('dashboard.index'))
    if pass_valid():
        return redirect(PATH)
    use_code = mfa.mfa_required() and mfa.is_enrolled(current_user)
    if request.method == 'POST':
        if _locked(current_user):
            flash('This account is locked for a while after too many failed attempts.', 'danger')
            return render_template('auth/sensehive_unlock.html', use_code=use_code), 429
        try:
            ok = (mfa.verify(current_user, request.form.get('code', '')) if use_code
                  else current_user.check_password(request.form.get('password', '')))
        except mfa.MFAUnavailable:
            flash('The two-factor service is not responding. Try again shortly.', 'danger')
            return render_template('auth/sensehive_unlock.html', use_code=use_code), 503
        if ok:
            current_user.failed_logins = 0
            from app import db
            db.session.commit()
            return _grant(redirect(PATH), 'after a 2FA code' if use_code else 'after the password')
        _record_failure(current_user, '2FA code (SenseHive)' if use_code else 'password (SenseHive)')
        log_activity('SenseHive unlock refused', status='warning')
        flash('That code is not valid.' if use_code else 'That password is not correct.', 'danger')
    return render_template('auth/sensehive_unlock.html', use_code=use_code)
