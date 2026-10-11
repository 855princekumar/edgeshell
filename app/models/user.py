from datetime import datetime
from flask_login import UserMixin
from app import db, bcrypt, login_manager

# Concurrent sessions an administrator may allow per account (phone, tablet,
# laptop, desktop). Every account, including the seeded admin, starts at 1.
MIN_SESSIONS = 1
MAX_SESSIONS = 4


def clamp_sessions(value, default=MIN_SESSIONS):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_SESSIONS, min(MAX_SESSIONS, n))


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(32), nullable=False, default='monitor')  # administrator, operator, monitor
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    last_login_ip = db.Column(db.String(45))
    failed_logins = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)

    # Live sessions this account may hold at once (1-4, set by an admin).
    max_sessions = db.Column(db.Integer, nullable=False, default=MIN_SESSIONS)
    sessions = db.relationship('UserSession', backref='user',
                               cascade='all, delete-orphan', passive_deletes=True)

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

    def _role(self):
        return (self.role or '').strip().lower()

    def is_admin(self):
        return self._role() == 'administrator'

    def is_operator(self):
        return self._role() in ('administrator', 'operator')

    def can_execute(self):
        return self._role() in ('administrator', 'operator')

    def can_upload(self):
        return self._role() in ('administrator', 'operator')

    def __repr__(self):
        return f'<User {self.username}>'


class UserSession(db.Model):
    """One signed-in browser. The token is also in that browser's signed session
    cookie; a request is accepted only while this row exists and is not revoked,
    so sessions can be ended server-side (logout, password reset, disable)."""
    __tablename__ = 'user_sessions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    ip = db.Column(db.String(45))
    agent = db.Column(db.String(256))
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    seen_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    revoked_at = db.Column(db.DateTime)
    revoked_reason = db.Column(db.String(32))   # replaced, logout, password_changed, ...
    revoked_detail = db.Column(db.String(256))  # e.g. the device that replaced it


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
