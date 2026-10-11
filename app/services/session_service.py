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

REVOKE_MESSAGES = {
    'password_changed': 'Your password was changed, so this session was signed out.',
    'password_reset': 'An administrator reset your password. Please sign in again.',
    'limit_lowered': ("An administrator lowered this account's session limit, "
                      'so this session was signed out.'),
    'admin_revoked': 'An administrator signed out your sessions. Please sign in again.',
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


def _idle():
    return current_app.config.get('PERMANENT_SESSION_LIFETIME', timedelta(hours=8))


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
    return {'id': s.id, 'ip': s.ip, 'agent': s.agent,
            'started_at': s.started_at, 'seen_at': s.seen_at}


def _revoke(rows, reason, detail=None):
    now = datetime.utcnow()
    for s in rows:
        s.revoked_at = now
        s.revoked_reason = reason
        s.revoked_detail = (detail or '')[:256] or None


def _prune(user):
    """Drop rows that can no longer matter (idle-expired or long revoked)."""
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
    db.session.add(UserSession(user_id=user.id, token=token, ip=_client_ip(),
                               agent=_agent_label(), started_at=now, seen_at=now))
    session['sid'] = token
    return replaced


def end_session(user):
    """Logout: delete this browser's session row."""
    sid = session.pop('sid', None)
    if user and sid:
        UserSession.query.filter_by(user_id=user.id, token=sid).delete()


def revoke_sessions(user, reason, keep_current=False):
    """Revoke every live session of a user (optionally keeping this browser's)."""
    rows = live_sessions(user)
    if keep_current:
        sid = session.get('sid')
        rows = [s for s in rows if s.token != sid]
    _revoke(rows, reason)
    return len(rows)


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
        if now - row.seen_at > SEEN_WRITE_EVERY:
            row.seen_at = now
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        return None
