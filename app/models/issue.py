"""Support / issue tickets.

Isolation model:
  - A reporter sees only their own tickets.
  - Administrators see every ticket.
Each ticket carries a threaded message log (the "registry" from reporter to
admin and back), like a small CRM conversation.
"""
from datetime import datetime
from app import db
from app.utils.timeutil import iso_utc
from app.models import LongText

ISSUE_STATUSES = ('open', 'in_progress', 'resolved', 'closed')
ISSUE_PRIORITIES = ('low', 'normal', 'high', 'urgent')


class Issue(db.Model):
    __tablename__ = 'issues'

    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True, nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(64))
    priority = db.Column(db.String(16), default='normal')
    status = db.Column(db.String(16), default='open', index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    reporter = db.relationship('User', backref='issues')
    messages = db.relationship('IssueMessage', backref='issue',
                               cascade='all, delete-orphan',
                               order_by='IssueMessage.created_at')

    def to_dict(self):
        return {
            'id': self.id,
            'subject': self.subject,
            'category': self.category,
            'priority': self.priority,
            'status': self.status,
            'reporter': self.reporter.username if self.reporter else None,
            'created_at': iso_utc(self.created_at),
            'updated_at': iso_utc(self.updated_at),
            'message_count': len(self.messages),
        }


class IssueMessage(db.Model):
    __tablename__ = 'issue_messages'

    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey('issues.id'), index=True, nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    author_name = db.Column(db.String(64))
    is_staff = db.Column(db.Boolean, default=False)  # authored by an admin
    body = db.Column(LongText, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'author': self.author_name,
            'is_staff': self.is_staff,
            'body': self.body,
            'created_at': iso_utc(self.created_at),
        }
