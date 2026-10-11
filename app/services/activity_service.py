from datetime import datetime
from flask import request
from flask_login import current_user
from app import db
from app.models import ActivityLog


def log_activity(action, node=None, command=None, response=None, status='success', duration=None):
    """Record a fleet activity to the database."""
    try:
        entry = ActivityLog(
            timestamp=datetime.utcnow(),
            user=current_user.username if current_user and current_user.is_authenticated else 'system',
            node=node,
            action=action,
            command=command,
            response=str(response)[:4000] if response else None,
            status=status,
            duration=duration,
            ip_address=request.remote_addr if request else None
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()
