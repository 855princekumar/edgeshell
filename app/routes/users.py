from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from app import db
from app.models.user import User, clamp_sessions, MIN_SESSIONS, MAX_SESSIONS
from app.services.activity_service import log_activity
from app.services.session_service import (enforce_limit, live_session_counts,
                                          revoke_sessions)
from functools import wraps

users_bp = Blueprint('users', __name__, url_prefix='/users')


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            flash('Administrator access required.', 'danger')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


@users_bp.route('/')
@login_required
@admin_required
def index():
    users = User.query.all()
    return render_template('users/index.html', users=users,
                           live_counts=live_session_counts(),
                           session_range=range(MIN_SESSIONS, MAX_SESSIONS + 1))


@users_bp.route('/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        role = request.form.get('role', 'monitor').strip().lower()
        if role not in ('administrator', 'operator', 'monitor'):
            role = 'monitor'
        password = request.form.get('password', '')
        max_sessions = clamp_sessions(request.form.get('max_sessions'))

        if not username:
            flash('Username is required.', 'danger')
        elif not email:
            flash('Email is required.', 'danger')
        elif User.query.filter(db.func.lower(User.username) == username.lower()).first():
            flash(f'Username "{username}" is already taken.', 'danger')
        elif User.query.filter(db.func.lower(User.email) == email.lower()).first():
            flash(f'Email "{email}" is already registered.', 'danger')
        elif len(password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
        else:
            user = User(username=username, email=email.lower(), role=role,
                        is_active=True, must_change_password=True,
                        max_sessions=max_sessions)
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.commit()
                log_activity(f'Add User: {username} (max sessions {max_sessions})',
                             status='success')
                flash(f'User "{username}" created.', 'success')
                return redirect(url_for('users.index'))
            except IntegrityError:
                db.session.rollback()
                flash('Could not create user: username or email already exists.',
                      'danger')

    return render_template('users/add.html',
                           session_range=range(MIN_SESSIONS, MAX_SESSIONS + 1))


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


@users_bp.route('/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('users.index'))
    name = user.username
    db.session.delete(user)
    db.session.commit()
    log_activity(f'Delete User: {name}', status='success')
    flash(f'User "{name}" deleted.', 'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        return jsonify({'error': 'Cannot toggle own account'}), 400
    user.is_active = not user.is_active
    if not user.is_active:
        revoke_sessions(user, 'deactivated')   # signed out now, not at cookie expiry
    db.session.commit()
    log_activity(f"{'Enable' if user.is_active else 'Disable'} User: {user.username}",
                 status='success')
    if request.accept_mimetypes.best == 'application/json' or request.is_json:
        return jsonify({'success': True, 'active': user.is_active})
    flash(f"User \"{user.username}\" {'enabled' if user.is_active else 'disabled and signed out'}.",
          'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/reset-password', methods=['POST'])
@login_required
@admin_required
def reset_password(user_id):
    user = User.query.get_or_404(user_id)
    new_password = request.form.get('new_password', '')
    if len(new_password) < 8:
        flash('Password must be at least 8 characters.', 'danger')
        return redirect(url_for('users.index'))
    user.set_password(new_password)
    user.must_change_password = True
    # A reset usually means the account may be compromised: end every session.
    revoke_sessions(user, 'password_reset', keep_current=(user.id == current_user.id))
    db.session.commit()
    log_activity(f'Reset Password: {user.username}', status='success')
    flash(f'Password reset for "{user.username}". Their sessions were signed out.', 'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/sessions', methods=['POST'])
@login_required
@admin_required
def set_sessions(user_id):
    """Set how many devices may be signed in at once (1-4)."""
    user = User.query.get_or_404(user_id)
    user.max_sessions = clamp_sessions(request.form.get('max_sessions'), user.max_sessions)
    dropped = enforce_limit(user)
    db.session.commit()
    log_activity(f'Set max sessions: {user.username} -> {user.max_sessions}',
                 status='success')
    flash(f'"{user.username}" may now be signed in on {user.max_sessions} device(s).'
          + (f' {dropped} older session(s) were signed out.' if dropped else ''), 'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/signout', methods=['POST'])
@login_required
@admin_required
def sign_out_all(user_id):
    """Sign a user out everywhere, e.g. after a suspected session hijack."""
    user = User.query.get_or_404(user_id)
    n = revoke_sessions(user, 'admin_revoked', keep_current=(user.id == current_user.id))
    db.session.commit()
    log_activity(f'Sign out sessions: {user.username} ({n})', status='warning')
    flash(f'Signed out {n} session(s) for "{user.username}".', 'success')
    return redirect(url_for('users.index'))


@users_bp.route('/<int:user_id>/role', methods=['POST'])
@login_required
@admin_required
def set_role(user_id):
    user = User.query.get_or_404(user_id)
    role = request.form.get('role', '').strip().lower()
    if role not in ('administrator', 'operator', 'monitor'):
        flash('Invalid role.', 'danger')
        return redirect(url_for('users.index'))
    if user.id == current_user.id and role != 'administrator':
        flash('You cannot remove your own administrator role.', 'warning')
        return redirect(url_for('users.index'))
    user.role = role
    db.session.commit()
    log_activity(f'Set role: {user.username} -> {role}', status='success')
    flash(f'Role for "{user.username}" set to {role}.', 'success')
    return redirect(url_for('users.index'))
