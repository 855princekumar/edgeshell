"""Apps on the edge (ADR 0017): every package uploaded is kept in the bucket
for the audit, every rollout and node action is recorded."""
import json
from datetime import datetime

from app import db
from app.models import LongText
from app.utils.timeutil import iso_utc


class AppPackage(db.Model):
    """One uploaded package (NAME-VERSION.esapp), kept on the server until an
    administrator removes it after the audit."""
    __tablename__ = 'app_packages'
    __table_args__ = (db.UniqueConstraint('name', 'version', name='uq_app_package_version'),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), nullable=False, index=True)
    version = db.Column(db.String(40), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    size = db.Column(db.BigInteger, default=0)
    manifest = db.Column(LongText)                    # JSON, as checked by esapp
    filename = db.Column(db.String(120))              # in the bucket: packages/NAME/VERSION/FILE
    uploaded_by = db.Column(db.String(64))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    note = db.Column(db.String(300))
    removed_at = db.Column(db.DateTime)               # file removed from the bucket; the record stays
    removed_by = db.Column(db.String(64))

    def manifest_dict(self):
        try:
            return json.loads(self.manifest or '{}')
        except ValueError:
            return {}

    def to_dict(self):
        m = self.manifest_dict()
        return {'id': self.id, 'name': self.name, 'version': self.version, 'sha256': self.sha256,
                'size': self.size, 'filename': self.filename, 'uploaded_by': self.uploaded_by,
                'uploaded_at': iso_utc(self.uploaded_at), 'note': self.note,
                'description': m.get('description') or '', 'run': ' '.join(m.get('run') or []),
                'ports': m.get('ports') or [], 'requirements': m.get('requirements'),
                'health': m.get('health') or {}, 'in_bucket': self.removed_at is None,
                'removed_at': iso_utc(self.removed_at), 'removed_by': self.removed_by}


class AppAction(db.Model):
    """One action on one or many nodes: a rollout, a rollback, a cleanup, an
    uninstall. Results per node are kept for the audit."""
    __tablename__ = 'app_actions'

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(16), nullable=False)    # rollout | activate | rollback | prune | uninstall
    app = db.Column(db.String(40), index=True)
    version = db.Column(db.String(40))
    package_id = db.Column(db.Integer, db.ForeignKey('app_packages.id'))
    by = db.Column(db.String(64))
    started_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    finished_at = db.Column(db.DateTime)
    status = db.Column(db.String(16), default='running')   # running | done | stopped | failed
    nodes = db.Column(LongText)                       # JSON list of node names
    results = db.Column(LongText)                     # JSON {node: {ok, message, rolled_back_to}}
    summary = db.Column(db.String(300))
    options = db.Column(db.String(300))               # JSON: canary, stop_after_pct, versions...

    def to_dict(self):
        def j(v, d):
            try:
                return json.loads(v) if v else d
            except ValueError:
                return d
        return {'id': self.id, 'kind': self.kind, 'app': self.app, 'version': self.version,
                'package_id': self.package_id, 'by': self.by, 'started_at': iso_utc(self.started_at),
                'finished_at': iso_utc(self.finished_at), 'status': self.status, 'nodes': j(self.nodes, []),
                'results': j(self.results, {}), 'summary': self.summary, 'options': j(self.options, {})}
