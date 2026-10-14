"""API credentials and the event feed.

ApiToken holds every API credential, stored only as a SHA-256 hash of a
high-entropy secret (the secret itself is shown once and never stored):

  challenge  a pending sign-in between the password and the 2FA code (5 minutes,
             a few code attempts)
  session    a signed-in API session (after password + 2FA); expires when idle
             and at an absolute limit, ends at logout or revocation
  key        a service key for unattended integrations, created by its owner
             after re-entering password and 2FA, with scopes, an expiry and an
             optional IP allowlist

NodeEvent is an append-only feed (status changes, paused and resumed logins)
that other systems poll with a cursor to drive their alerts and triggers.
"""
from datetime import datetime

from app import db


class ApiToken(db.Model):
    __tablename__ = 'api_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    kind = db.Column(db.String(12), nullable=False, index=True)      # challenge | session | key
    name = db.Column(db.String(80))                                  # keys: what it is for
    prefix = db.Column(db.String(16), nullable=False, unique=True, index=True)
    secret_hash = db.Column(db.String(64), nullable=False)
    scopes = db.Column(db.String(64), nullable=False, default='read')  # read,write,control,admin
    allowed_ips = db.Column(db.String(512))                           # keys: CIDRs, comma separated
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_ip = db.Column(db.String(45))
    expires_at = db.Column(db.DateTime, nullable=False)
    last_used_at = db.Column(db.DateTime)
    last_used_ip = db.Column(db.String(45))
    attempts = db.Column(db.Integer, default=0)                       # challenge: wrong codes
    revoked_at = db.Column(db.DateTime)
    revoked_reason = db.Column(db.String(32))

    user = db.relationship('User')

    def scope_set(self):
        return {s for s in (self.scopes or '').split(',') if s}

    def public(self):
        """Safe description (never the secret or its hash)."""
        return {'id': self.id, 'kind': self.kind, 'name': self.name, 'prefix': self.prefix,
                'scopes': sorted(self.scope_set()), 'allowed_ips': self.allowed_ips or None,
                'created_at': _iso(self.created_at), 'expires_at': _iso(self.expires_at),
                'last_used_at': _iso(self.last_used_at), 'last_used_ip': self.last_used_ip,
                'revoked': self.revoked_at is not None, 'revoked_reason': self.revoked_reason,
                'user': self.user.username if self.user else None}


class NodeEvent(db.Model):
    __tablename__ = 'node_events'

    id = db.Column(db.Integer, primary_key=True)                     # the cursor
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    node_id = db.Column(db.Integer, index=True)                      # no FK: events outlive nodes
    node_name = db.Column(db.String(64))
    kind = db.Column(db.String(32), nullable=False, index=True)       # status_changed, login_paused, ...
    old_value = db.Column(db.String(32))
    new_value = db.Column(db.String(32))
    message = db.Column(db.String(256))

    def to_dict(self):
        return {'id': self.id, 'timestamp': _iso(self.timestamp), 'node_id': self.node_id,
                'node_name': self.node_name, 'kind': self.kind, 'from': self.old_value,
                'to': self.new_value, 'message': self.message}


def _iso(t):
    return t.isoformat(timespec='seconds') + 'Z' if t else None


class LakeHeartbeat(db.Model):
    """One row, written on the hot database every minute and read back from
    the data lake: the replication lag, measured end to end."""
    __tablename__ = 'lake_heartbeat'

    id = db.Column(db.Integer, primary_key=True)
    beat = db.Column(db.DateTime)
