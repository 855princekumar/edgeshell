from datetime import datetime
from sqlalchemy.dialects.mysql import LONGTEXT
from app import db
from app.utils.timeutil import iso_utc

# TEXT on MariaDB caps at 64 KB; logs and deploy results can exceed that.
LongText = db.Text().with_variant(LONGTEXT(), 'mysql', 'mariadb')


class ActivityLog(db.Model):
    __tablename__ = 'activity_logs'

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user = db.Column(db.String(64))
    node = db.Column(db.String(64))
    action = db.Column(db.String(128))
    command = db.Column(LongText)
    response = db.Column(LongText)
    status = db.Column(db.String(16))  # success, failure, warning
    duration = db.Column(db.Float)  # seconds
    ip_address = db.Column(db.String(45))
    device = db.Column(db.String(160))  # 'Mobile #12 (Safari on iPhone)'

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': iso_utc(self.timestamp),
            'user': self.user,
            'device': self.device,
            'node': self.node,
            'action': self.action,
            'command': self.command,
            'response': self.response,
            'status': self.status,
            'duration': self.duration,
            'ip_address': self.ip_address,
        }


class NodeLog(db.Model):
    __tablename__ = 'node_logs'

    id = db.Column(db.Integer, primary_key=True)
    node_name = db.Column(db.String(64), index=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    log_source = db.Column(db.String(128))
    log_path = db.Column(db.String(256))
    severity = db.Column(db.String(16))  # info, warning, error, critical
    raw_log = db.Column(LongText)
    formatted_log = db.Column(LongText)
    collection_time = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'node_name': self.node_name,
            'timestamp': iso_utc(self.timestamp),
            'log_source': self.log_source,
            'log_path': self.log_path,
            'severity': self.severity,
            'raw_log': self.raw_log,
            'formatted_log': self.formatted_log,
            'collection_time': iso_utc(self.collection_time),
        }


class Deployment(db.Model):
    __tablename__ = 'deployments'

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user = db.Column(db.String(64))
    filename = db.Column(db.String(256))
    target_path = db.Column(db.String(256))
    nodes = db.Column(LongText)  # JSON list
    status = db.Column(db.String(16))  # pending, running, success, partial, failed
    success_count = db.Column(db.Integer, default=0)
    failure_count = db.Column(db.Integer, default=0)
    result = db.Column(LongText)  # JSON result per node
    duration = db.Column(db.Float)

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': iso_utc(self.timestamp),
            'user': self.user,
            'filename': self.filename,
            'target_path': self.target_path,
            'nodes': self.nodes,
            'status': self.status,
            'success_count': self.success_count,
            'failure_count': self.failure_count,
            'result': self.result,
            'duration': self.duration,
        }


class SystemMetrics(db.Model):
    __tablename__ = 'system_metrics'

    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, db.ForeignKey('nodes.id'))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    cpu_percent = db.Column(db.Float)
    ram_percent = db.Column(db.Float)
    disk_percent = db.Column(db.Float)
    load_avg_1 = db.Column(db.Float)
    load_avg_5 = db.Column(db.Float)
    load_avg_15 = db.Column(db.Float)
    temperature = db.Column(db.Float)
    uptime_seconds = db.Column(db.Integer)
    network_rx = db.Column(db.BigInteger)
    network_tx = db.Column(db.BigInteger)
    docker_running = db.Column(db.Boolean)
    nodered_running = db.Column(db.Boolean)

    node = db.relationship('Node', backref='metrics')


class VpnStatus(db.Model):
    __tablename__ = 'vpn_status'

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    connected = db.Column(db.Boolean, default=False)
    ip_address = db.Column(db.String(45))
    node_count = db.Column(db.Integer, default=0)
    nodes_json = db.Column(LongText)
    backend_state = db.Column(db.String(64))


class Settings(db.Model):
    __tablename__ = 'settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(128), unique=True, nullable=False, index=True)
    value = db.Column(LongText)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<Settings {self.key}={self.value}>'


class ServiceEvent(db.Model):
    """Service lifecycle audit: every start/stop of the EdgeShell service.

    Written by run.py (start, before Gunicorn boots) and by systemd's
    ExecStopPost (stop, with the unit result). Lets you see host reboots,
    crashes, watchdog restarts and deploys from phpMyAdmin or the Logs page.
    """
    __tablename__ = 'service_events'

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)  # UTC
    event = db.Column(db.String(16), index=True)      # start | stop
    reason = db.Column(db.String(64))                  # first_start, host_boot, service_restart, watchdog, deploy, manual_stop, crash
    detail = db.Column(db.String(512))
    result = db.Column(db.String(32))                  # systemd SERVICE_RESULT on stop
    exit_code = db.Column(db.String(32))
    exit_status = db.Column(db.String(32))
    boot_id = db.Column(db.String(64), index=True)
    hostname = db.Column(db.String(128))
    pid = db.Column(db.Integer)
    version = db.Column(db.String(32))
    host_uptime_s = db.Column(db.Integer)

    def to_dict(self):
        return {c.name: (iso_utc(getattr(self, c.name)) if c.name == 'timestamp'
                         else getattr(self, c.name))
                for c in self.__table__.columns}
