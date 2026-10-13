"""Database export / import for painless SQLite -> Postgres/MariaDB moves.

Model-driven, so it is engine-agnostic. Export walks every SQLAlchemy model and
writes rows to a single JSON snapshot. Import replays them into whatever
DATABASE_URL currently points at, in FK-safe order. This is the "export/import"
migration mode: dump on the old DB, point DATABASE_URL at the new one, load.

Encrypted fields (node passwords) are exported as stored ciphertext, so the
snapshot is only as portable as the CREDENTIAL_KEY. Keep the key with the dump.
"""
import json
from datetime import datetime, date

from app import db
from app.models.user import PasswordResetRequest, User, UserMFA, UserSession
from app.models.node import Node
from app.models import (ActivityLog, NodeLog, Deployment, SystemMetrics,
                        VpnStatus, Settings, ServiceEvent)
from app.models.issue import Issue, IssueMessage

# FK-safe order: users/nodes first, then dependent tables.
# 2FA rows move with the users (privacyIDEA serials, or secrets encrypted with
# the same CREDENTIAL_KEY), so nobody has to enroll again after a migration.
EXPORT_ORDER = [User, UserMFA, PasswordResetRequest, Node, Settings, Issue,
                IssueMessage, ActivityLog, NodeLog, Deployment, SystemMetrics,
                VpnStatus, ServiceEvent]
# Live sessions are not exported (everyone signs in again after a migration),
# but they reference users, so they are cleared before users are wiped.
WIPE_FIRST = [UserSession]


def _col_names(model):
    return [c.name for c in model.__table__.columns]


def _attr_map(model):
    """column name -> mapped attribute key. Node's 'password' column maps to
    '_password' (ciphertext); reading the column name would hit the decrypting
    property and leak plaintext into the snapshot."""
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(model)
    return {c.name: mapper.get_property_by_column(c).key
            for c in model.__table__.columns}


def _serialize(v):
    if isinstance(v, (datetime, date)):
        return {'__dt__': v.isoformat()}
    return v


def _deserialize(v):
    if isinstance(v, dict) and '__dt__' in v:
        return datetime.fromisoformat(v['__dt__'])
    return v


def export_snapshot(path):
    snapshot = {'version': 1, 'created_at': datetime.utcnow().isoformat(),
                'tables': {}}
    for model in EXPORT_ORDER:
        cols = _col_names(model)
        amap = _attr_map(model)
        rows = []
        for obj in db.session.query(model).all():
            rows.append({c: _serialize(getattr(obj, amap[c])) for c in cols})
        snapshot['tables'][model.__tablename__] = {'columns': cols, 'rows': rows}
    with open(path, 'w') as f:
        json.dump(snapshot, f, indent=2)
    return {t: len(v['rows']) for t, v in snapshot['tables'].items()}


def _fit(model, row):
    """MariaDB is strict about VARCHAR length; SQLite never was. Trim values
    that would overflow their column instead of failing the whole import."""
    for col in model.__table__.columns:
        v = row.get(col.name)
        length = getattr(col.type, 'length', None)
        if length and isinstance(v, str) and len(v) > length:
            row[col.name] = v[:length]
    return row


def import_snapshot(path, wipe=False, bulk=False):
    """bulk=True: fast executemany INSERT for an empty target (migration)."""
    with open(path) as f:
        snapshot = json.load(f)
    counts = {}
    is_mysql = db.engine.dialect.name == 'mysql'
    if is_mysql:
        db.session.execute(db.text('SET FOREIGN_KEY_CHECKS=0'))
    if bulk:
        for model in WIPE_FIRST + list(reversed(EXPORT_ORDER)):
            db.session.query(model).delete()
        for model in EXPORT_ORDER:
            table = snapshot['tables'].get(model.__tablename__)
            if not table:
                continue
            valid = set(_col_names(model))
            rows = [_fit(model, {k: _deserialize(v) for k, v in r.items() if k in valid})
                    for r in table['rows']]
            for i in range(0, len(rows), 1000):
                if rows[i:i + 1000]:
                    db.session.execute(model.__table__.insert(), rows[i:i + 1000])
            counts[model.__tablename__] = len(rows)
        if is_mysql:  # restore on the same connection before it returns to the pool
            db.session.execute(db.text('SET FOREIGN_KEY_CHECKS=1'))
        db.session.commit()
        return counts
    if wipe:
        for model in WIPE_FIRST + list(reversed(EXPORT_ORDER)):
            db.session.query(model).delete()
        db.session.flush()
    for model in EXPORT_ORDER:
        table = snapshot['tables'].get(model.__tablename__)
        if not table:
            continue
        valid = set(_col_names(model))
        amap = _attr_map(model)
        for row in table['rows']:
            data = _fit(model, {k: _deserialize(v) for k, v in row.items() if k in valid})
            data = {amap[k]: v for k, v in data.items()}   # ciphertext stays ciphertext
            # merge = insert-or-update by primary key. Idempotent, so it is safe
            # against a target DB that already seeded defaults, and re-runnable.
            db.session.merge(model(**data))
        counts[model.__tablename__] = len(table['rows'])
    if is_mysql:
        db.session.execute(db.text('SET FOREIGN_KEY_CHECKS=1'))
    db.session.commit()
    return counts
