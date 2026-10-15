"""Per-user session limits with server-side revocation.

How it works
  - Each sign-in creates a row in user_sessions holding a random token; the
    same token goes into the browser's signed session cookie (session['sid']).
  - Every request looks the token up. A missing, revoked or idle-expired row
    signs this browser out with the reason.
  - An account may hold users.max_sessions live sessions (1 to 4, default 1,
    set by an administrator). Signing in beyond the limit asks to sign out the
    least recently active device first.
  - Logout, password change or reset, and disabling an account revoke
    sessions server-side, so a copied cookie stops working immediately.

  - Each session records its device type and when a person last interacted
    with it (active_at: page loads, clicks, key presses; background polls do
    not count). After IDLE_MINUTES without interaction the page asks "still
    there?" and signs that session out unless answered within
    IDLE_WARNING_MINUTES. The server enforces the same deadline, so a closed
    laptop or a forgotten tab is signed out too.

Tokens live in the database, so this holds across Gunicorn threads, restarts
and hosts. SINGLE_SESSION=false lifts the per-user limit; sessions are still
tracked and revocable.
"""
import secrets
from datetime import datetime, timedelta

from flask import (current_app, flash, jsonify, redirect, request, session,
                   url_for)
from flask_login import current_user, logout_user

from app import db
from app.models.user import UserSession, clamp_sessions

SEEN_WRITE_EVERY = timedelta(seconds=60)   # throttle last-seen writes
# session_status is NOT exempt: its poll is how an idle tab learns it was replaced.
SESSION_EXEMPT_ENDPOINTS = {'static', 'health.healthz'}
ACTIVE_WRITE_EVERY = timedelta(seconds=20)  # throttle interaction writes
# Pages a signed-in user may still open while a required step is pending.
GATE_OPEN_ENDPOINTS = {'auth.logout', 'auth.change_password', 'auth.session_status',
                       'auth.session_activity', 'account.email', 'account.email_verify',
                       'account.email_resend', 'static',
                       'health.healthz'}

REVOKE_MESSAGES = {
    'password_changed': 'Your password was changed, so this session was signed out.',
    'password_reset': 'An administrator reset your password. Please sign in again.',
    'limit_lowered': ("An administrator lowered this account's session limit, "
                      'so this session was signed out.'),
    'admin_revoked': 'An administrator signed out your sessions. Please sign in again.',
    'closed': 'This session was closed from another of your devices or by an administrator.',
    'idle': 'You were signed out after a period of inactivity.',
    'mfa_reset': 'Your two-factor authentication was reset. Please sign in again.',
    'deactivated': 'This account was disabled.',
    'role_changed': 'Your role changed. Please sign in again.',
}


def _client_ip():
    return (request.remote_addr or '')[:45]


_BROWSERS = (('Edg/', 'Edge'), ('OPR/', 'Opera'), ('Firefox/', 'Firefox'),
             ('Chrome/', 'Chrome'), ('Safari/', 'Safari'), ('curl/', 'curl'))
_SYSTEMS = (('Windows', 'Windows'), ('iPhone', 'iPhone'), ('iPad', 'iPad'),
            ('Android', 'Android'), ('Mac OS X', 'macOS'), ('CrOS', 'ChromeOS'),
            ('Linux', 'Linux'))


def _agent_label():
    """'Chrome on Windows' style label (Werkzeug 3 no longer parses UAs)."""
    raw = request.headers.get('User-Agent', '') or ''
    browser = next((n for k, n in _BROWSERS if k in raw), None)
    system = next((n for k, n in _SYSTEMS if k in raw), None)
    if browser or system:
        return f"{browser or 'Browser'} on {system or 'unknown OS'}"[:256]
    return (raw or 'Unknown device')[:256]


def device_type():
    raw = request.headers.get('User-Agent', '') or ''
    if 'iPad' in raw or 'Tablet' in raw or ('Android' in raw and 'Mobile' not in raw):
        return 'Tablet'
    if 'Mobi' in raw or 'iPhone' in raw:
        return 'Mobile'
    return 'Desktop'


def device_label(s):
    """'Desktop #12 (Chrome on Windows)' for logs and session lists."""
    return f"{s.device_type or 'Device'} #{s.id} ({s.agent or 'unknown'})"


def _idle():
    return current_app.config.get('PERMANENT_SESSION_LIFETIME', timedelta(hours=8))


def idle_limits():
    """(seconds without interaction before the prompt, seconds to answer it).
    (0, 0) disables the inactivity sign-out."""
    idle = int(current_app.config.get('IDLE_MINUTES', 10)) * 60
    warn = int(current_app.config.get('IDLE_WARNING_MINUTES', 5)) * 60
    return (idle, warn) if idle > 0 else (0, 0)


def current_session():
    sid = session.get('sid')
    if not sid or not current_user.is_authenticated:
        return None
    return UserSession.query.filter_by(user_id=current_user.id, token=sid).first()


def mark_active(row, now=None):
    now = now or datetime.utcnow()
    if not row.active_at or now - row.active_at > ACTIVE_WRITE_EVERY:
        row.active_at = now
        return True
    return False


def _is_interaction():
    """A page the person asked for, not a background poll from a page."""
    if request.endpoint in ('auth.session_status', 'auth.session_activity'):
        return False
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return False
    if request.headers.get('Accept', '').startswith('application/json'):
        return False     # fetch() from a page: polls and button actions alike
    return request.method != 'GET' or request.accept_mimetypes.accept_html


def session_limit(user):
    """Live sessions allowed for this user, or None when limits are off."""
    if not current_app.config.get('SINGLE_SESSION', True):
        return None
    return clamp_sessions(user.max_sessions)


def live_sessions(user):
    """Unrevoked, non-idle sessions, least recently active first."""
    cutoff = datetime.utcnow() - _idle()
    return (UserSession.query
            .filter(UserSession.user_id == user.id,
                    UserSession.revoked_at.is_(None),
                    UserSession.seen_at >= cutoff)
            .order_by(UserSession.seen_at.asc())
            .all())


def sessions_to_replace(user):
    """Sessions a new sign-in would push out (oldest first), as dicts."""
    limit = session_limit(user)
    live = live_sessions(user)
    if limit is None or len(live) < limit:
        return []
    return [_describe(s) for s in live[:len(live) - limit + 1]]


def _describe(s):
    return {'id': s.id, 'ip': s.ip, 'agent': s.agent, 'device_type': s.device_type,
            'started_at': s.started_at, 'seen_at': s.seen_at,
            'active_at': s.active_at or s.started_at}


def _revoke(rows, reason, detail=None):
    now = datetime.utcnow()
    for s in rows:
        s.revoked_at = now
        s.revoked_reason = reason
        s.revoked_detail = (detail or '')[:256] or None


def _prune(user):
    """Drop rows that can no longer matter (idle-expired or long revoked).
    With a data lake the collector does this on the hot database only, so the
    lake keeps every session for the audit trail."""
    from app.services import lake
    if lake.configured():
        return
    cutoff = datetime.utcnow() - _idle()
    (UserSession.query
     .filter(UserSession.user_id == user.id,
             db.or_(UserSession.seen_at < cutoff, UserSession.revoked_at < cutoff))
     .delete(synchronize_session=False))


def start_session(user):
    """Create this browser's session, signing out the oldest ones over the
    limit. Returns the replaced sessions (dicts) for the audit message."""
    _prune(user)
    limit = session_limit(user)
    live = live_sessions(user)
    over = live[:len(live) - limit + 1] if limit is not None and len(live) >= limit else []
    replaced = [_describe(s) for s in over]
    _revoke(over, 'replaced', f'{_agent_label()}, {_client_ip()}')

    token = secrets.token_hex(32)
    now = datetime.utcnow()
    row = UserSession(user_id=user.id, token=token, ip=_client_ip(),
                      agent=_agent_label(), device_type=device_type(),
                      started_at=now, seen_at=now, active_at=now)
    db.session.add(row)
    db.session.flush()
    session['sid'] = token
    session['sid_label'] = device_label(row)
    return replaced


def end_session(user):
    """Logout: end this browser's session row (deleted without a data lake;
    with one it is marked signed out, and housekeeping removes it from the hot
    database only)."""
    from app.services import lake
    sid = session.pop('sid', None)
    if user and sid:
        q = UserSession.query.filter_by(user_id=user.id, token=sid)
        if lake.configured():
            q.filter(UserSession.revoked_at.is_(None)).update(
                {'revoked_at': datetime.utcnow(), 'revoked_reason': 'logout'}, synchronize_session=False)
        else:
            q.delete()


def revoke_sessions(user, reason, keep_current=False):
    """Revoke every live session of a user (optionally keeping this browser's)."""
    rows = live_sessions(user)
    if keep_current:
        sid = session.get('sid')
        rows = [s for s in rows if s.token != sid]
    _revoke(rows, reason)
    # API sessions end with the browser sessions. Service keys also end when
    # the account may be compromised or is closed (an admin reset, a 2FA
    # reset, deactivation, "sign out everywhere"); a user's own routine
    # password change keeps their keys so integrations do not break.
    from app.services import api_auth
    kinds = ('session', 'challenge')
    if reason in ('password_reset', 'mfa_reset', 'deactivated', 'admin_revoked'):
        kinds = ('session', 'challenge', 'key')
    api_auth.revoke_all_for(user, reason, kinds=kinds)
    return len(rows)


def close_session(user, session_id, reason='closed'):
    """End one of a user's sessions (e.g. the phone left signed in)."""
    row = UserSession.query.filter_by(id=session_id, user_id=user.id).first()
    if not row or row.revoked_at is not None:
        return None
    _revoke([row], reason)
    return row


def enforce_limit(user):
    """After an admin lowers max_sessions, sign out the oldest extras."""
    limit = session_limit(user)
    live = live_sessions(user)
    if limit is None or len(live) <= limit:
        return 0
    extra = live[:len(live) - limit]
    _revoke(extra, 'limit_lowered')
    return len(extra)


def live_session_counts():
    """{user_id: live session count} for the user management page."""
    cutoff = datetime.utcnow() - _idle()
    rows = (db.session.query(UserSession.user_id, db.func.count(UserSession.id))
            .filter(UserSession.revoked_at.is_(None), UserSession.seen_at >= cutoff)
            .group_by(UserSession.user_id).all())
    return dict(rows)


def _revoked_response(reason=None, detail=None):
    from app.services.activity_service import log_activity
    if reason == 'idle':
        log_activity('Signed out (inactive)', response=session.get('sid_label'),
                     status='info')
    if reason == 'replaced':
        log_activity('Session revoked (signed in on another device)',
                     response=f'new session from {detail}', status='warning')
        msg = ('You were signed out because this account signed in on another '
               f'device ({detail}). This account allows '
               f'{clamp_sessions(current_user.max_sessions)} active session(s).')
    else:
        msg = REVOKE_MESSAGES.get(reason, 'Your session has ended. Please sign in again.')
    logout_user()
    session.clear()
    wants_json = (request.is_json or request.path.startswith('/api')
                  or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                  or 'application/json' in request.headers.get('Accept', ''))
    if wants_json:
        return jsonify({'error': 'session_revoked', 'message': msg,
                        'login_url': url_for('auth.login')}), 401
    flash(msg, 'warning')
    return redirect(url_for('auth.login'))


def register_single_session(app):
    @app.before_request
    def _enforce_sessions():
        if request.endpoint in SESSION_EXEMPT_ENDPOINTS:
            return None
        from flask import g
        if getattr(g, 'api_token', None) is not None or getattr(g, 'api_bearer_present', False):
            return None                      # API bearer credential: checked in api_auth
        if not current_user.is_authenticated:
            return None
        sid = session.get('sid')
        row = (UserSession.query.filter_by(user_id=current_user.id, token=sid).first()
               if sid else None)

        # Disabled accounts never get here: Flask-Login reports them as not
        # authenticated, and disabling revokes their rows so re-enabling the
        # account does not revive an old cookie.
        now = datetime.utcnow()
        if row is None or now - row.seen_at > _idle():
            return _revoked_response()
        if row.revoked_at is not None:
            return _revoked_response(row.revoked_reason, row.revoked_detail)

        idle, warn = idle_limits()
        if idle and now - (row.active_at or row.started_at) > timedelta(seconds=idle + warn):
            _revoke([row], 'idle')
            db.session.commit()
            return _revoked_response('idle')

        dirty = False
        if now - row.seen_at > SEEN_WRITE_EVERY:
            row.seen_at = now
            dirty = True
        if _is_interaction():
            dirty = mark_active(row, now) or dirty
        if dirty:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        # Required steps before the rest of the app opens.
        # An administrator stuck at email verification can still reach the mail
        # settings, or a broken mail setup would lock everyone out for good.
        admin_mail_fix = request.endpoint == 'issues.email_settings' and current_user.is_admin()
        if request.endpoint not in GATE_OPEN_ENDPOINTS and not admin_mail_fix:
            if current_user.needs_recovery_email():
                return _gate('account.email', 'Add and verify your email address before '
                             'continuing: password reset links are sent there.')
            if current_user.must_change_password:
                return _gate('auth.change_password',
                             'Please set a new password before continuing.')
        return None


def _gate(endpoint, message):
    if request.headers.get('Accept', '').startswith('application/json') or request.is_json:
        return jsonify({'error': 'action_required', 'message': message,
                        'url': url_for(endpoint)}), 403
    flash(message, 'warning')
    return redirect(url_for(endpoint))
