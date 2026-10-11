"""Central role-based access control.

Roles (least to most privilege):
  monitor       read-only: dashboards, monitoring, logs
  operator      monitor + node control, command execution, uploads, fleet ops
  administrator operator + user management, VPN config, settings

Sessions are already isolated per user by Flask-Login's signed cookie, so
concurrent logins are independent. These decorators enforce *what* each role
may do, consistently, in one place.
"""
from functools import wraps
from flask import flash, redirect, url_for, jsonify, request
from flask_login import current_user

ROLE_RANK = {'monitor': 0, 'operator': 1, 'administrator': 2}


def _rank(role):
    return ROLE_RANK.get((role or '').strip().lower(), -1)


def _deny():
    # JSON for API/AJAX callers, redirect for browser navigation.
    if request.accept_mimetypes.best == 'application/json' or \
            request.path.startswith('/api') or request.is_json:
        return jsonify({'error': 'forbidden', 'message': 'Insufficient role'}), 403
    flash('You do not have permission to do that.', 'danger')
    return redirect(url_for('dashboard.index'))


def role_required(minimum):
    """Require at least `minimum` role rank."""
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated or \
                    _rank(current_user.role) < _rank(minimum):
                return _deny()
            return f(*args, **kwargs)
        return decorated
    return wrapper


def admin_required(f):
    return role_required('administrator')(f)


def operator_required(f):
    return role_required('operator')(f)
