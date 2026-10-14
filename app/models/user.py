from datetime import datetime
from flask_login import UserMixin
from app import db, bcrypt, login_manager

# Concurrent sessions an administrator may allow per account (phone, tablet,
# laptop, desktop). Every account, including the seeded admin, starts at 1.
MIN_SESSIONS = 1
MAX_SESSIONS = 4

# At most this many administrators. The owner (the first administrator) is the
# only one who may promote another account to administrator.
MAX_ADMINS = 3
ROLES = ('administrator', 'operator', 'monitor')


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

    # Self sign-up: 'pending' until an administrator approves it with a role.
    approval_status = db.Column(db.String(16), nullable=False, default='approved')
    # The first administrator. Only the owner promotes users to administrator;
    # ownership passes to the longest-serving admin when the owner steps down.
    is_owner = db.Column(db.Boolean, nullable=False, default=False)
    admin_since = db.Column(db.DateTime)

    # Live sessions this account may hold at once (1-4, set by an admin).
    max_sessions = db.Column(db.Integer, nullable=False, default=MIN_SESSIONS)
    # Page settings the user chose (JSON), kept until they change them: across
    # pages, sign-outs and devices. Keys are whitelisted in PREFS.
    ui_prefs = db.Column(db.Text)
    # An administrator can block a user from the API (all credentials end).
    api_access = db.Column(db.Boolean, nullable=False, default=True)
    sessions = db.relationship('UserSession', backref='user',
                               cascade='all, delete-orphan', passive_deletes=True)

    # key -> (type, default)
    PREFS = {'monitoring_auto_refresh': (bool, True)}

    def get_pref(self, key):
        import json
        kind, default = self.PREFS[key]
        try:
            value = json.loads(self.ui_prefs or '{}').get(key, default)
        except ValueError:
            value = default
        return value if isinstance(value, kind) else default

    def set_pref(self, key, value):
        import json
        kind, _ = self.PREFS[key]
        if not isinstance(value, kind):
            raise ValueError(key)
        try:
            prefs = json.loads(self.ui_prefs or '{}')
        except ValueError:
            prefs = {}
        prefs[key] = value
        self.ui_prefs = json.dumps(prefs)

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

    def _role(self):
        return (self.role or '').strip().lower()

    def is_admin(self):
        return self._role() == 'administrator'

    def is_pending(self):
        return self.approval_status == 'pending'

    def needs_recovery_email(self):
        """Administrators must keep a personal email for reset links."""
        email = (self.email or '').lower()
        return self.is_admin() and (not email or email.endswith('.local')
                                    or email.endswith('@example.com'))

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
    device_type = db.Column(db.String(16))      # Desktop, Mobile, Tablet
    # Last real interaction (page load, click, key press). seen_at also moves
    # on background polls; active_at only when a person is using the page.
    active_at = db.Column(db.DateTime)


class UserMFA(db.Model):
    """A user's second factor. Only references live here when privacyIDEA
    checks the codes (token serials); with the built-in verifier the TOTP
    secret is encrypted with CREDENTIAL_KEY and backup codes are hashed."""
    __tablename__ = 'user_mfa'

    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                        primary_key=True)
    provider = db.Column(db.String(16), nullable=False)   # privacyidea | local
    totp_ref = db.Column(db.Text, nullable=False)
    backup_ref = db.Column(db.Text)
    backup_total = db.Column(db.Integer, default=0)
    confirmed_at = db.Column(db.DateTime)                  # null until first code
    last_step = db.Column(db.BigInteger, default=0)        # replay guard (local)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('mfa', uselist=False,
                                                      cascade='all, delete-orphan',
                                                      passive_deletes=True))


class PasswordResetRequest(db.Model):
    """'Forgot password' from a non-admin account, waiting for an admin."""
    __tablename__ = 'password_reset_requests'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    ip = db.Column(db.String(45))
    status = db.Column(db.String(16), nullable=False, default='open')  # open, done, dismissed
    handled_by = db.Column(db.String(64))
    handled_at = db.Column(db.DateTime)

    user = db.relationship('User', backref=db.backref('reset_requests',
                                                      cascade='all, delete-orphan',
                                                      passive_deletes=True))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
