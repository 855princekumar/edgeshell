"""Hardware lifecycle (ADR 0015).

A node is a position in the fleet (name, location, role); the hardware that
serves it changes over time. Tables:

  hw_components  one physical part (board, SD card or disk, network adapter,
                 OS install, power supply...) and its service period on a node
  hw_changes     a hardware change the collector detected, waiting for an
                 operator to confirm it as a replacement (or reject it)
  hw_faults      a fault report on a node: component, symptom, status, and how
                 it was resolved
  hw_events      the hardware timeline of every node (append only)
  webhooks       outgoing notifications to other software, signed with HMAC
  webhook_deliveries  the last delivery attempts, for troubleshooting

node_id columns have no foreign key: the history outlives a node that is
deleted for good, and node_name keeps it readable.
"""
from datetime import datetime

from app import db
from app.models import LongText
from app.services.crypto import decrypt_value, encrypt_value
from app.utils.timeutil import iso_utc

DETECTED_KINDS = ('board', 'storage', 'network', 'os')
KIND_LABELS = {'board': 'Board', 'storage': 'SD card or disk', 'network': 'Network adapter',
               'os': 'OS install', 'power': 'Power supply'}
FAULT_STATUSES = ('open', 'in_progress', 'resolved')
FAULT_SEVERITIES = ('low', 'normal', 'high', 'critical')
RESOLUTIONS = ('replaced', 'repaired', 'reseated', 'no_fault_found', 'other')
CHANGE_STATUSES = ('pending', 'confirmed', 'rejected', 'reverted')
CHANGE_REASONS = ('fault', 'upgrade', 'maintenance', 'other')
REMOVED_REASONS = CHANGE_REASONS + ('moved', 'retired')


class HardwareComponent(db.Model):
    __tablename__ = 'hw_components'
    __table_args__ = (db.Index('ix_hw_components_kind_identifier', 'kind', 'identifier'),)

    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, index=True)
    node_name = db.Column(db.String(64))
    kind = db.Column(db.String(32), nullable=False)             # board, storage, network, os, or a fault category
    identifier = db.Column(db.String(128))                       # serial, card ID, MAC address, machine ID
    model = db.Column(db.String(128))
    source = db.Column(db.String(12), default='detected')        # detected | manual
    installed_at = db.Column(db.DateTime, default=datetime.utcnow)
    installed_known = db.Column(db.Boolean, default=True)        # False: in service before EdgeShell first saw it
    removed_at = db.Column(db.DateTime, index=True)              # None: in service
    removed_reason = db.Column(db.String(16))
    last_seen = db.Column(db.DateTime)

    def to_dict(self):
        end = self.removed_at or datetime.utcnow()
        return {'id': self.id, 'node_id': self.node_id, 'node_name': self.node_name, 'kind': self.kind,
                'label': KIND_LABELS.get(self.kind, self.kind), 'identifier': self.identifier,
                'model': self.model, 'source': self.source, 'installed_at': iso_utc(self.installed_at),
                'installed_known': bool(self.installed_known), 'removed_at': iso_utc(self.removed_at),
                'removed_reason': self.removed_reason, 'in_service': self.removed_at is None,
                'service_days': round((end - self.installed_at).total_seconds() / 86400, 1) if self.installed_at else None}


class HardwareChange(db.Model):
    __tablename__ = 'hw_changes'

    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, index=True)
    node_name = db.Column(db.String(64))
    detected_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    status = db.Column(db.String(12), default='pending', index=True)
    summary = db.Column(db.String(160))
    changes = db.Column(LongText)                                # JSON list of {kind, old, new, ...}
    decided_by = db.Column(db.String(64))
    decided_at = db.Column(db.DateTime)
    reason = db.Column(db.String(16))                            # why the hardware was changed
    note = db.Column(db.String(500))

    def items(self):
        import json
        try:
            return json.loads(self.changes or '[]')
        except ValueError:
            return []

    def to_dict(self):
        return {'id': self.id, 'node_id': self.node_id, 'node_name': self.node_name,
                'detected_at': iso_utc(self.detected_at), 'status': self.status, 'summary': self.summary,
                'changes': self.items(), 'decided_by': self.decided_by, 'decided_at': iso_utc(self.decided_at),
                'reason': self.reason, 'note': self.note}


class HardwareFault(db.Model):
    __tablename__ = 'hw_faults'

    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, index=True)
    node_name = db.Column(db.String(64), index=True)
    component = db.Column(db.String(32), nullable=False, index=True)
    symptom = db.Column(db.String(160), nullable=False)
    description = db.Column(LongText)
    severity = db.Column(db.String(12), default='normal')
    status = db.Column(db.String(12), default='open', index=True)
    reported_by = db.Column(db.String(64))
    reported_via = db.Column(db.String(8), default='web')        # web | api | auto
    started_at = db.Column(db.DateTime)                          # when the fault began, if known
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, index=True)
    resolved_by = db.Column(db.String(64))
    resolution = db.Column(db.String(16))
    resolution_note = db.Column(db.String(500))
    new_serial = db.Column(db.String(128))
    downtime_min = db.Column(db.Integer)                         # node fully down between start and fix
    undervoltage_hint = db.Column(db.Boolean, default=False)     # the board reported low power beforehand

    def to_dict(self):
        start = self.started_at or self.created_at
        fix = ((self.resolved_at - start).total_seconds() / 3600) if self.resolved_at and start else None
        return {'id': self.id, 'node_id': self.node_id, 'node_name': self.node_name, 'component': self.component,
                'symptom': self.symptom, 'description': self.description, 'severity': self.severity,
                'status': self.status, 'reported_by': self.reported_by, 'reported_via': self.reported_via,
                'started_at': iso_utc(self.started_at), 'created_at': iso_utc(self.created_at),
                'updated_at': iso_utc(self.updated_at), 'resolved_at': iso_utc(self.resolved_at),
                'resolved_by': self.resolved_by, 'resolution': self.resolution,
                'resolution_note': self.resolution_note, 'new_serial': self.new_serial,
                'downtime_min': self.downtime_min, 'hours_to_fix': round(fix, 2) if fix is not None else None,
                'undervoltage_hint': bool(self.undervoltage_hint)}


class HardwareEvent(db.Model):
    __tablename__ = 'hw_events'

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    node_id = db.Column(db.Integer, index=True)
    node_name = db.Column(db.String(64))
    kind = db.Column(db.String(32), nullable=False)
    user = db.Column(db.String(64))
    message = db.Column(db.String(300))
    fault_id = db.Column(db.Integer, index=True)
    change_id = db.Column(db.Integer)

    def to_dict(self):
        return {'id': self.id, 'at': iso_utc(self.at), 'node_id': self.node_id, 'node_name': self.node_name,
                'kind': self.kind, 'user': self.user, 'message': self.message, 'fault_id': self.fault_id,
                'change_id': self.change_id}


class Webhook(db.Model):
    __tablename__ = 'webhooks'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    _secret = db.Column('secret', db.Text, nullable=False)
    events = db.Column(db.String(500), nullable=False)           # comma separated, or '*'
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.String(64))
    last_status = db.Column(db.Integer)
    last_at = db.Column(db.DateTime)
    failures = db.Column(db.Integer, default=0)                  # in a row

    @property
    def secret(self):
        return decrypt_value(self._secret)

    @secret.setter
    def secret(self, value):
        self._secret = encrypt_value(value)

    def event_set(self):
        return {e for e in (self.events or '').split(',') if e}

    def to_dict(self):
        """Never the secret."""
        return {'id': self.id, 'name': self.name, 'url': self.url, 'events': sorted(self.event_set()),
                'active': bool(self.active), 'created_at': iso_utc(self.created_at), 'created_by': self.created_by,
                'last_status': self.last_status, 'last_at': iso_utc(self.last_at), 'failures': self.failures or 0}


class WebhookDelivery(db.Model):
    __tablename__ = 'webhook_deliveries'

    id = db.Column(db.Integer, primary_key=True)
    webhook_id = db.Column(db.Integer, index=True)
    delivery = db.Column(db.String(36))
    event = db.Column(db.String(40))
    at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    status_code = db.Column(db.Integer)
    attempts = db.Column(db.Integer, default=1)
    error = db.Column(db.String(200))
    duration_ms = db.Column(db.Integer)

    def to_dict(self):
        return {'id': self.id, 'webhook_id': self.webhook_id, 'delivery': self.delivery, 'event': self.event,
                'at': iso_utc(self.at), 'status_code': self.status_code, 'attempts': self.attempts,
                'error': self.error, 'duration_ms': self.duration_ms}
