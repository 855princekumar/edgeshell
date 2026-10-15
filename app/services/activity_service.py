from datetime import datetime
from flask import has_request_context, request, session
from flask_login import current_user
from app import db
from app.models import ActivityLog


def log_activity(action, node=None, command=None, response=None, status='success', duration=None, user=None):
    """Record a fleet activity to the database. `user` names the person when
    there is no request (background jobs started by someone)."""
    try:
        entry = ActivityLog(
            timestamp=datetime.utcnow(),
            user=user or (current_user.username if has_request_context() and current_user
                          and current_user.is_authenticated else 'system'),
            node=node,
            action=action,
            command=command,
            response=str(response)[:4000] if response else None,
            status=status,
            duration=duration,
            ip_address=request.remote_addr if has_request_context() else None,
            device=(session.get('sid_label') or None) if has_request_context() else None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()
