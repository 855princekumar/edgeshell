from datetime import datetime
from app import db
from app.utils.timeutil import iso_utc
from app.services.crypto import encrypt_value, decrypt_value


class Node(db.Model):
    __tablename__ = 'nodes'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)
    ssh_port = db.Column(db.Integer, default=22)
    username = db.Column(db.String(64), nullable=False)
    _password = db.Column('password', db.Text)
    description = db.Column(db.Text)
    tags = db.Column(db.String(256))
    environment = db.Column(db.String(32))  # production, staging, dev
    location = db.Column(db.String(128))
    node_type = db.Column(db.String(32))  # edge, gateway, sensor, server
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_seen = db.Column(db.DateTime)
    status = db.Column(db.String(16), default='unknown')  # online, offline, auth_failed, unknown
    last_error = db.Column(db.String(256))        # why the last check failed, shown on the card
    vpn_status = db.Column(db.String(16), default='unknown')
    arch = db.Column(db.String(32))               # armv7l, aarch64, x86_64
    nodered_port = db.Column(db.Integer, default=1880)
    orgui_installed = db.Column(db.Boolean, default=None)   # None=unknown
    orgui_port = db.Column(db.Integer, default=6080)
    orgui_checked_at = db.Column(db.DateTime)
    docker_installed = db.Column(db.Boolean, default=None)
    nodered_installed = db.Column(db.Boolean, default=None)
    capabilities_checked_at = db.Column(db.DateTime)
    # MQTT heartbeat agent (second, VPN-independent signal, via SenseHive)
    hb_installed = db.Column(db.Boolean, default=None)          # None=unknown
    hb_checked_at = db.Column(db.DateTime)
    hb_status = db.Column(db.String(16))                        # online | offline (last will) | silent | None
    hb_last_seen = db.Column(db.DateTime)
    hb_since = db.Column(db.DateTime)
    hb_lwt_at = db.Column(db.DateTime)
    hb_agent_version = db.Column(db.String(16))
    hb_way = db.Column(db.String(24))                           # how the agent reaches the broker, e.g. ws-tls:8884
    hb_routine_at = db.Column(db.DateTime)                      # the routine's last action on this node
    hb_routine_note = db.Column(db.String(200))                 # what it did ("failed ..." when it did not work)
    is_deleted = db.Column(db.Boolean, default=False, index=True)  # recycle bin
    deleted_at = db.Column(db.DateTime)
    # Hardware lifecycle (ADR 0015): what serves this position, and its lineage
    hw_fingerprint = db.Column(db.String(64))         # hash of the last hardware identity seen
    hw_scanned_at = db.Column(db.DateTime)
    hw_model = db.Column(db.String(128))              # e.g. the board model
    hw_os = db.Column(db.String(128))
    hw_undervoltage_at = db.Column(db.DateTime)       # the board last reported low power
    hw_alert = db.Column(db.Boolean, default=False)   # a hardware change was rejected as unexpected
    hw_replace_prompt = db.Column(db.String(12))      # pending | dismissed | done (new nodes)
    replaces_id = db.Column(db.Integer)               # the node this one replaced
    replaced_by_id = db.Column(db.Integer)
    retired_at = db.Column(db.DateTime)               # replaced by another node; kept with its history

    # Login policy: after failed logins EdgeShell stops logging in to the node
    # (no password guessing, no lockouts) until the cause is fixed. See
    # app/services/login_policy.py.
    login_paused_at = db.Column(db.DateTime)
    login_pause_reason = db.Column(db.String(16))     # password | tailscale | unknown
    ssh_banner = db.Column(db.String(80))             # SSH server banner seen at the pause

    # Cached metrics
    cpu_percent = db.Column(db.Float)
    ram_percent = db.Column(db.Float)
    disk_percent = db.Column(db.Float)
    load_average = db.Column(db.String(32))
    uptime = db.Column(db.String(64))
    temperature = db.Column(db.Float)

    @property
    def password(self):
        if self._password:
            return decrypt_value(self._password)
        return None

    @password.setter
    def password(self, value):
        if value:
            self._password = encrypt_value(value)
        else:
            self._password = None

    @property
    def arch_bits(self):
        """Return '64', '32', or '' from the reported machine architecture."""
        a = (self.arch or '').lower()
        if a in ('aarch64', 'arm64', 'x86_64', 'amd64', 'x64', 'ppc64le'):
            return '64'
        if a in ('armv6l', 'armv7l', 'armhf', 'armv7', 'i386', 'i486', 'i586', 'i686', 'x86'):
            return '32'
        return ''

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'ip_address': self.ip_address,
            'ssh_port': self.ssh_port,
            'username': self.username,
            'description': self.description,
            'tags': self.tags,
            'environment': self.environment,
            'location': self.location,
            'node_type': self.node_type,
            'is_active': self.is_active,
            'last_seen': iso_utc(self.last_seen),
            'status': self.status,
            'last_error': self.last_error,
            'vpn_status': self.vpn_status,
            'cpu_percent': self.cpu_percent,
            'ram_percent': self.ram_percent,
            'disk_percent': self.disk_percent,
            'load_average': self.load_average,
            'uptime': self.uptime,
            'arch': self.arch,
            'arch_bits': self.arch_bits,
            'nodered_port': self.nodered_port or 1880,
            'orgui_installed': self.orgui_installed,
            'orgui_port': self.orgui_port or 6080,
            'orgui_checked_at': iso_utc(self.orgui_checked_at),
            'docker_installed': self.docker_installed,
            'nodered_installed': self.nodered_installed,
            'is_deleted': self.is_deleted,
            'deleted_at': iso_utc(self.deleted_at),
            'login_paused': self.login_paused_at is not None,
            'login_paused_at': iso_utc(self.login_paused_at),
            'login_pause_reason': self.login_pause_reason,
            'hardware': {'model': self.hw_model, 'os': self.hw_os, 'scanned_at': iso_utc(self.hw_scanned_at),
                         'undervoltage_at': iso_utc(self.hw_undervoltage_at), 'alert': bool(self.hw_alert),
                         'replaces_id': self.replaces_id, 'replaced_by_id': self.replaced_by_id,
                         'retired_at': iso_utc(self.retired_at)},
            'heartbeat': {'installed': self.hb_installed, 'status': self.hb_status,
                          'last_seen': iso_utc(self.hb_last_seen), 'since': iso_utc(self.hb_since),
                          'lwt_at': iso_utc(self.hb_lwt_at), 'agent_version': self.hb_agent_version,
                          'way': self.hb_way, 'routine_at': iso_utc(self.hb_routine_at),
                          'routine_note': self.hb_routine_note},
        }

    def __repr__(self):
        return f'<Node {self.name}>'
