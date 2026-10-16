"""User management for administrators.

Rules live in app/services/account_service.py: at most 5 administrators;
the owner (primary administrator) manages every other account, admins
included; other administrators manage monitors and operators only.
"""
from functools import wraps

from flask import (Blueprint, current_app, flash, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app import db
from app.models.user import (MAX_SESSIONS, MIN_SESSIONS, PasswordResetRequest,
                             User, UserMFA, UserSession, clamp_sessions)
from app.services import account_service as accounts
from app.services.activity_service import log_activity
from app.services.session_service import (close_session, device_label,
                                          enforce_limit, live_session_counts,
                                          live_sessions, revoke_sessions)

users_bp = Blueprint('users', __name__, url_prefix='/users')

PROTECTED = ('Only the owner (primary administrator) can change another administrator, '
             'and nobody can change the owner.')


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash('Administrator access required.', 'danger')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


def _target(user_id, allow_self=False):
    """The user an admin action applies to, or (None, response) when the
    rules forbid it."""
    user = User.query.get_or_404(user_id)
    if allow_self and user.id == current_user.id:
        return user, None
    if user.id == current_user.id:
        flash('Use My account to change your own account.', 'warning')
        return None, redirect(url_for('users.index'))
    if not accounts.can_manage(current_user, user):
        flash(PROTECTED, 'danger')
        return None, redirect(url_for('users.index'))
    return user, None


def _wants_json():
    return request.accept_mimetypes.best == 'application/json' or request.is_json


@users_bp.route('/')
@login_required
@admin_required
def index():
    users = User.query.filter(User.approval_status != 'pending').order_by(User.id).all()
    pending = (User.query.filter_by(approval_status='pending')
               .order_by(User.created_at.asc()).all())
    enrolled = {r.user_id for r in UserMFA.query.filter(UserMFA.confirmed_at.isnot(None))}
    return render_template('users/index.html', users=users, pending=pending,
                           requests=accounts.open_requests(), enrolled=enrolled,
                           live_counts=live_session_counts(),
                           session_range=range(MIN_SESSIONS, MAX_SESSIONS + 1),
                           grantable=accounts.grantable_roles(current_user),
                           admin_count=accounts.admin_count(),
                           max_admins=accounts.MAX_ADMINS,
                           can_manage=lambda u: accounts.can_manage(current_user, u))


@users_bp.route('/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        role = request.form.get('role', 'monitor').strip().lower()
        password = request.form.get('password', '')
        max_sessions = clamp_sessions(request.form.get('max_sessions'))
        problem = accounts.check_role_grant(current_user, role)

        from app.routes.auth import USERNAME_RE
        if not USERNAME_RE.match(username):
            flash('Usernames are 3 to 64 letters, digits, dots, dashes or underscores.', 'danger')
        elif not email:
            flash('Email is required.', 'danger')
        elif problem:
            flash(problem, 'danger')
        elif User.query.filter(db.func.lower(User.username) == username.lower()).first():
            flash(f'Username "{username}" is already taken.', 'danger')
        elif User.query.filter(db.func.lower(User.email) == email.lower()).first():
            flash(f'Email "{email}" is already registered.', 'danger')
        elif len(password) < 10:
            flash('Temporary passwords must be at least 10 characters.', 'danger')
        else:
            user = User(username=username, email=email.lower(), role='monitor',
                        is_active=True, must_change_password=True,
                        max_sessions=max_sessions)
            accounts.apply_role(user, role)
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.commit()
                log_activity(f'Add User: {username} as {role} (max sessions {max_sessions})',
                             status='success')
                flash(f'User "{username}" created. They set a new password and two-factor '
                      'authentication at first sign-in.', 'success')
                return redirect(url_for('users.index'))
            except IntegrityError:
                db.session.rollback()
                flash('Could not create user: username or email already exists.',
                      'danger')

    return render_template('users/add.html',
                           session_range=range(MIN_SESSIONS, MAX_SESSIONS + 1),
                           grantable=accounts.grantable_roles(current_user),
                           max_admins=accounts.MAX_ADMINS)


@users_bp.route('/check')
@login_required
@admin_required
def check_availability():
    """Live availability check for the add-user form. Case-insensitive."""
    username = request.args.get('username', '').strip()
    email = request.args.get('email', '').strip()
    resp = {}
    if username:
        resp['username_taken'] = db.session.query(
            User.query.filter(db.func.lower(User.username) == username.lower()).exists()
        ).scalar()
    if email:
        resp['email_taken'] = db.session.query(
            User.query.filter(db.func.lower(User.email) == email.lower()).exists()
        ).scalar()
    return jsonify(resp)


# ---------------------------------------------------------------- sign-ups

@users_bp.route('/<int:user_id>/approve', methods=['POST'])
@login_required
@admin_required
def approve(user_id):
    user = User.query.get_or_404(user_id)
    if not user.is_pending():
        flash(f'"{user.username}" is not waiting for approval.', 'info')
        return redirect(url_for('users.index'))
    role = request.form.get('role', 'monitor').strip().lower()
    problem = accounts.check_role_grant(current_user, role)
    if problem:
        flash(problem, 'danger')
        return redirect(url_for('users.index'))
    user.approval_status = 'approved'
    user.is_active = True
    user.max_sessions = clamp_sessions(request.form.get('max_sessions'))
    accounts.apply_role(user, role)
    db.session.commit()
    log_activity(f'Approve sign-up: {user.username} as {role}', status='success')
    flash(f'"{user.username}" approved as {role}. They set up two-factor authentication '
          'at first sign-in.', 'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/reject', methods=['POST'])
@login_required
@admin_required
def reject(user_id):
    user = User.query.get_or_404(user_id)
    if not user.is_pending():
        flash(f'"{user.username}" is not waiting for approval.', 'info')
        return redirect(url_for('users.index'))
    name = user.username
    refused = _delete_user_row(user)
    if refused:
        flash(refused, 'danger')
        return redirect(url_for('users.index'))
    log_activity(f'Reject sign-up: {name}', status='warning')
    flash(f'Sign-up request from "{name}" rejected and removed.', 'success')
    return redirect(url_for('users.index'))


def _delete_user_row(user):
    """Remove the account from the hot database; the data lake keeps it
    (with its sessions, API credentials and history). The username and email
    are first changed on both databases so they can be used again. Returns
    an error message, or None."""
    from app.models.issue import Issue, IssueMessage
    from app.services import api_auth, lake
    from app.services.session_service import revoke_sessions
    from sqlalchemy.exc import IntegrityError
    if Issue.query.filter_by(reporter_id=user.id).first() or IssueMessage.query.filter_by(author_id=user.id).first():
        return f'"{user.username}" has support tickets on record, so the account cannot be deleted. Deactivate it instead.'
    try:
        lake.require_ready()                    # refuse before changing anything
    except lake.LakeNotReady as e:
        return f'Not deleted: the data lake must be caught up first ({lake.refusal_detail()}).'
    revoke_sessions(user, 'deactivated')
    api_auth.revoke_all_for(user, 'deleted')
    uid = user.id
    user.username = lake.tombstone(user.username, uid)
    user.email = f'deleted-{uid}~{user.email}'[:120]
    user.is_active = False
    db.session.commit()
    try:
        lake.hot_only([('DELETE FROM users WHERE id = :i', {'i': uid})])
    except lake.LakeNotReady as e:
        return f'The account was disabled but not deleted: the data lake must be caught up first ({lake.refusal_detail()}).'
    except IntegrityError:
        db.session.rollback()
        return 'The account was disabled but not deleted: it gained records meanwhile. Deactivate it instead.'
    return None


# ---------------------------------------------------------------- account actions

@users_bp.route('/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete(user_id):
    user, deny = _target(user_id)
    if deny:
        return deny
    name = user.username
    from app.services import lake
    from app.services.mfa_service import MFAUnavailable, remove
    from app.models.issue import Issue, IssueMessage
    if Issue.query.filter_by(reporter_id=user.id).first() or IssueMessage.query.filter_by(author_id=user.id).first():
        flash(f'"{name}" has support tickets on record, so the account cannot be deleted. Deactivate it instead.',
              'danger')
        return redirect(url_for('users.index'))
    try:
        lake.require_ready()                    # nothing (2FA included) is touched on a refusal
    except lake.LakeNotReady as e:
        flash(f'Not deleted: the data lake must be caught up first ({lake.refusal_detail()}).', 'danger')
        return redirect(url_for('users.index'))
    try:
        remove(user)
    except MFAUnavailable:
        pass
    refused = _delete_user_row(user)
    if refused:
        flash(refused, 'danger')
        return redirect(url_for('users.index'))
    log_activity(f'Delete User: {name}', status='success')
    flash(f'User "{name}" deleted.', 'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle(user_id):
    user, deny = _target(user_id)
    if deny:
        return (jsonify({'error': PROTECTED}), 403) if _wants_json() else deny
    user.is_active = not user.is_active
    if not user.is_active:
        revoke_sessions(user, 'deactivated')   # signed out now, not at cookie expiry
    db.session.commit()
    log_activity(f"{'Enable' if user.is_active else 'Disable'} User: {user.username}",
                 status='success')
    if _wants_json():
        return jsonify({'success': True, 'active': user.is_active})
    flash(f"User \"{user.username}\" {'enabled' if user.is_active else 'disabled and signed out'}.",
          'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/reset-password', methods=['POST'])
@login_required
@admin_required
def reset_password(user_id):
    """Temporary password + 2FA removed + every session ended."""
    user, deny = _target(user_id)
    if deny:
        return deny
    new_password = request.form.get('new_password', '')
    if len(new_password) < 10:
        flash('Temporary passwords must be at least 10 characters.', 'danger')
        return redirect(url_for('users.index'))
    accounts.admin_reset(user, new_password, current_user)
    db.session.commit()
    log_activity(f'Reset password and 2FA: {user.username}', status='warning')
    flash(f'Password and two-factor authentication reset for "{user.username}". Give them the '
          'temporary password; they choose a new one and set up 2FA at next sign-in.',
          'success')
    return redirect(url_for('users.index'))


@users_bp.route('/requests/<int:request_id>/dismiss', methods=['POST'])
@login_required
@admin_required
def dismiss_request(request_id):
    req = PasswordResetRequest.query.get_or_404(request_id)
    req.status = 'dismissed'
    from datetime import datetime
    req.handled_by, req.handled_at = current_user.username, datetime.utcnow()
    db.session.commit()
    log_activity(f'Dismiss reset request: {req.user.username}', status='info')
    flash(f'Reset request from "{req.user.username}" dismissed.', 'info')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/sessions', methods=['POST'])
@login_required
@admin_required
def set_sessions(user_id):
    """Set how many devices may be signed in at once (1-4)."""
    user, deny = _target(user_id, allow_self=True)
    if deny:
        return deny
    user.max_sessions = clamp_sessions(request.form.get('max_sessions'), user.max_sessions)
    dropped = enforce_limit(user)
    db.session.commit()
    log_activity(f'Set max sessions: {user.username} -> {user.max_sessions}',
                 status='success')
    flash(f'"{user.username}" may now be signed in on {user.max_sessions} device(s).'
          + (f' {dropped} older session(s) were signed out.' if dropped else ''), 'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/devices')
@login_required
@admin_required
def devices(user_id):
    user, deny = _target(user_id, allow_self=True)
    if deny:
        return deny
    from datetime import datetime
    rows = live_sessions(user)
    rows.sort(key=lambda s: s.active_at or s.started_at, reverse=True)
    recent = (UserSession.query.filter(UserSession.user_id == user.id,
                                       UserSession.revoked_at.isnot(None))
              .order_by(UserSession.revoked_at.desc()).limit(10).all())
    return render_template('users/devices.html', user=user, rows=rows, recent=recent,
                           device_label=device_label, now=datetime.utcnow())


@users_bp.route('/<int:user_id>/devices/<int:session_id>/close', methods=['POST'])
@login_required
@admin_required
def close_device(user_id, session_id):
    user, deny = _target(user_id)
    if deny:
        return deny
    row = close_session(user, session_id, reason='admin_revoked')
    if row:
        db.session.commit()
        log_activity(f'Close session: {user.username}', response=device_label(row),
                     status='warning')
        flash(f'Signed {user.username} out of {device_label(row)}.', 'success')
    return redirect(url_for('users.devices', user_id=user.id))


@users_bp.route('/<int:user_id>/signout', methods=['POST'])
@login_required
@admin_required
def sign_out_all(user_id):
    """Sign a user out everywhere, e.g. after a suspected session hijack."""
    user, deny = _target(user_id)
    if deny:
        return deny
    n = revoke_sessions(user, 'admin_revoked')
    db.session.commit()
    log_activity(f'Sign out sessions: {user.username} ({n})', status='warning')
    flash(f'Signed out {n} session(s) for "{user.username}".', 'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/role', methods=['POST'])
@login_required
@admin_required
def set_role(user_id):
    user, deny = _target(user_id)
    if deny:
        return deny
    role = request.form.get('role', '').strip().lower()
    problem = accounts.check_role_grant(current_user, role)
    if problem:
        flash(problem, 'danger')
        return redirect(url_for('users.index'))
    accounts.apply_role(user, role)
    db.session.commit()
    log_activity(f'Set role: {user.username} -> {role}', status='success')
    flash(f'Role for "{user.username}" set to {role}.', 'success')
    return redirect(url_for('users.index'))


# ---------------------------------------------------------------- API access (administrators)

@users_bp.route('/api')
@login_required
@admin_required
def api_access():
    """API policy, per-user API access, every credential, and the usage log."""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from app.models.api import ApiRequest, ApiToken
    from app.services import api_auth, lake
    q_user = (request.args.get('user') or '').strip()[:64]
    q_status = request.args.get('result') if request.args.get('result') in ('ok', 'refused', 'error') else ''
    now = datetime.utcnow()
    creds = (ApiToken.query.filter(ApiToken.kind.in_(('session', 'key')))
             .order_by(ApiToken.revoked_at.isnot(None), ApiToken.created_at.desc()).limit(300).all())
    with lake.reader() as r:
        log = r.session.query(ApiRequest)
        if q_user:
            log = log.filter(ApiRequest.username == q_user)
        if q_status == 'ok':
            log = log.filter(ApiRequest.status < 400)
        elif q_status == 'refused':
            log = log.filter(ApiRequest.status >= 400, ApiRequest.status < 500)
        elif q_status == 'error':
            log = log.filter(ApiRequest.status >= 500)
        log_rows = [x.to_dict() for x in log.order_by(ApiRequest.id.desc()).limit(300).all()]
        usage = (r.session.query(ApiRequest.username, func.count(ApiRequest.id),
                                 func.sum(db.case((ApiRequest.status >= 400, 1), else_=0)))
                 .filter(ApiRequest.timestamp >= now - timedelta(hours=24))
                 .group_by(ApiRequest.username).order_by(func.count(ApiRequest.id).desc()).limit(50).all())
        source = r.source
    return render_template('users/api.html', policy=api_auth.policy(), scopes=api_auth.SCOPES,
                           users=User.query.order_by(User.username).all(), creds=creds, now=now,
                           log_rows=log_rows, usage=usage, source=source, q_user=q_user, q_status=q_status,
                           commands_allowed=bool(current_app.config.get('API_ALLOW_COMMANDS')),
                           can_manage=accounts.can_manage)


@users_bp.route('/api/policy', methods=['POST'])
@login_required
@admin_required
def api_policy():
    from app.services import api_auth
    if not current_user.is_owner:
        flash('Only the owner can change the API policy.', 'danger')
        return redirect(url_for('users.api_access'))
    f = request.form
    scopes = [s for s in f.getlist('scopes') if s in api_auth.SCOPES]
    api_auth.save_policy(bool(f.get('enabled')), scopes, bool(f.get('keys')))
    p = api_auth.policy()
    log_activity('API policy changed', status='warning',
                 response=f"API {'on' if p['enabled'] else 'off'}; allowed: {', '.join(p['scopes']) or 'nothing'}; "
                          f"service keys {'allowed' if p['keys'] else 'not allowed'}")
    flash('API policy saved. It applies to the next request.', 'success')
    return redirect(url_for('users.api_access'))


@users_bp.route('/api/user/<int:user_id>/access', methods=['POST'])
@login_required
@admin_required
def api_user_access(user_id):
    from app.services import api_auth
    allow = request.form.get('allow') == '1'
    # Blocking yourself is fine; lifting a block on yourself is not, or an
    # administrator could undo the owner's decision about them.
    user, deny = _target(user_id, allow_self=not allow)
    if deny:
        return deny
    user.api_access = allow
    if not allow:
        api_auth.revoke_all_for(user, 'api_blocked')
    db.session.commit()
    log_activity(f"API access {'allowed' if allow else 'blocked'}: {user.username}", status='warning' if not allow else 'success',
                 response='' if allow else 'all API sessions and keys revoked')
    flash(f"API access {'allowed' if allow else 'blocked'} for {user.username}"
          + ('' if allow else '; every API session and key of this account was revoked.'), 'success')
    return redirect(url_for('users.api_access'))


@users_bp.route('/api/credential/<int:token_id>/revoke', methods=['POST'])
@login_required
@admin_required
def api_revoke(token_id):
    from app.models.api import ApiToken
    from app.services import api_auth
    row = ApiToken.query.filter(ApiToken.id == token_id, ApiToken.kind.in_(('session', 'key'))).first_or_404()
    owner = row.user
    if owner and owner.id != current_user.id and not accounts.can_manage(current_user, owner):
        flash(PROTECTED, 'danger')
        return redirect(url_for('users.api_access'))
    api_auth.revoke(row, 'revoked_by_admin')
    db.session.commit()
    log_activity('API credential revoked by an administrator', status='warning',
                 response=f'{row.kind} {row.prefix}' + (f' "{row.name}"' if row.name else '')
                 + f' of {owner.username if owner else "?"}')
    flash('Revoked. It stops working immediately.', 'success')
    return redirect(url_for('users.api_access'))
