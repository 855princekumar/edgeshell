"""Account governance rules, in one place so routes cannot drift.

  - At most MAX_ADMINS (3) administrators.
  - The owner (the first, primary administrator) has full control: grants
    and removes the administrator role, and changes, resets, signs out,
    disables or deletes any other account, administrators included.
  - Other administrators manage monitors and operators only; they cannot
    change another administrator, and nobody can change the owner.
    Administrators also leave the role themselves, confirming with their
    password and a 2FA code.
  - When the owner steps down, ownership passes to the longest-serving
    remaining administrator. The last administrator cannot step down.
  - An administrator reset of a user's password also removes that user's 2FA,
    which must be set up again at the next sign-in.
"""
from datetime import datetime

from app import db
from app.models.user import MAX_ADMINS, ROLES, PasswordResetRequest, User


def admins():
    return (User.query.filter(User.role == 'administrator',
                              User.approval_status == 'approved')
            .order_by(User.admin_since.asc(), User.id.asc()).all())


def admin_count():
    return len(admins())


def owner():
    return User.query.filter_by(is_owner=True).first()


def can_manage(actor, target):
    """Whether actor (an administrator) may change target's account."""
    if not actor.is_admin() or target.id == actor.id or target.is_owner:
        return False
    if actor.is_owner:
        return True                 # the primary admin manages everyone else
    return not target.is_admin()


def can_grant_admin(actor):
    return bool(actor.is_owner and actor.is_admin() and admin_count() < MAX_ADMINS)


def grantable_roles(actor):
    roles = ['monitor', 'operator']
    if can_grant_admin(actor):
        roles.append('administrator')
    return roles


def check_role_grant(actor, role):
    """None when allowed, else the reason."""
    if role not in ROLES:
        return 'Invalid role.'
    if role != 'administrator':
        return None
    if not actor.is_owner:
        return 'Only the owner (the first administrator) can grant the administrator role.'
    if admin_count() >= MAX_ADMINS:
        return f'There can be at most {MAX_ADMINS} administrators.'
    return None


def apply_role(user, role):
    became_admin = role == 'administrator' and not user.is_admin()
    user.role = role
    if became_admin:
        user.admin_since = datetime.utcnow()
    elif role != 'administrator':
        user.admin_since = None
        user.is_owner = False


def step_down(user, new_role):
    """The user leaves the administrator role. Returns (ok, message, successor)."""
    if not user.is_admin():
        return False, 'You are not an administrator.', None
    if new_role not in ('operator', 'monitor'):
        return False, 'Choose operator or monitor as your new role.', None
    others = [a for a in admins() if a.id != user.id]
    if not others:
        return False, ('You are the only administrator. Add another administrator '
                       'before stepping down.'), None
    successor = None
    if user.is_owner:
        successor = others[0]           # longest-serving remaining admin
        successor.is_owner = True
    apply_role(user, new_role)
    return True, None, successor


def admin_reset(user, new_password, actor):
    """Admin-initiated reset: new temporary password, 2FA removed, every
    session ended, open reset requests closed."""
    from app.services.mfa_service import remove as remove_mfa
    from app.services.session_service import revoke_sessions
    user.set_password(new_password)
    user.must_change_password = True
    user.failed_logins = 0
    user.locked_until = None
    remove_mfa(user)
    revoke_sessions(user, 'password_reset')
    close_requests(user, actor.username, 'done')


def close_requests(user, handled_by, status):
    now = datetime.utcnow()
    for r in PasswordResetRequest.query.filter_by(user_id=user.id, status='open'):
        r.status, r.handled_by, r.handled_at = status, handled_by, now


def open_requests():
    return (PasswordResetRequest.query.filter_by(status='open')
            .order_by(PasswordResetRequest.requested_at.asc()).all())


def ensure_owner():
    """Startup repair: every approved admin gets admin_since, and exactly one
    owner exists while there is any administrator (the earliest one)."""
    changed = False
    for a in admins():
        if not a.admin_since:
            a.admin_since = a.created_at or datetime.utcnow()
            changed = True
    current = admins()
    owners = [a for a in current if a.is_owner]
    if current and not owners:
        current[0].is_owner = True
        changed = True
    for extra in owners[1:]:
        extra.is_owner = False
        changed = True
    for u in User.query.filter(User.is_owner.is_(True), User.role != 'administrator'):
        u.is_owner = False
        changed = True
    if changed:
        db.session.commit()
