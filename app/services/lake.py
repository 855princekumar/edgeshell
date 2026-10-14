"""Hot database and data lake.

The primary MariaDB is the hot database: the app reads and writes it. When
LAKE_DATABASE_URL is set, a second MariaDB (the data lake) is a live replica
of it (Docker stack: the `mariadb-lake` service, set up by
docker/lake-init.sh) that keeps everything for good: metrics, rollups,
events, the activity log, accounts and API activity.

  Reads of history (exports, history and activity queries, the event feed)
  go to the lake, so heavy queries never slow down the fleet. If the lake is
  unreachable or behind, they fall back to the hot database.

  Purging the hot database (raw-metric retention, and the optional hot
  retention policy) runs with `sql_log_bin=0`: the delete is not written to
  the binary log, so the replica never sees it and keeps the rows. A purge
  only runs while the lake is confirmed caught up; otherwise nothing is
  deleted.

Replication health is measured end to end: the app writes a heartbeat row on
the hot database every minute and reads it back from the lake.
"""
import time
from datetime import datetime, timedelta

from flask import current_app

from app import db

HEARTBEAT_EVERY_S = 60
MAX_READ_LAG_S = 600            # reads fall back to the hot DB beyond this
MAX_PURGE_LAG_S = 180           # purges wait while the lake is further behind

PERIODS = {'week': 7, 'month': 30, 'quarter': 91, 'semester': 182, 'year': 365}
RETENTION_SETTING = 'hot_retention'
# table, time column: the history tables the hot retention policy may trim
RETAINED = (('system_metrics', 'timestamp'), ('metrics_hourly', 'hour'), ('activity_logs', 'timestamp'),
            ('node_events', 'timestamp'), ('node_logs', 'timestamp'), ('service_events', 'timestamp'))

_engine = None
_engine_url = None
_last_beat = 0.0


def configured():
    return bool(current_app.config.get('LAKE_DATABASE_URL'))


def engine():
    global _engine, _engine_url
    url = current_app.config.get('LAKE_DATABASE_URL')
    if not url:
        return None
    if _engine is None or _engine_url != url:
        from sqlalchemy import create_engine
        kw = {} if url.startswith('sqlite') else {'pool_size': 3, 'max_overflow': 2}
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800, **kw)
        _engine_url = url
    return _engine


def beat(force=False):
    """Write the heartbeat on the hot database (at most once a minute)."""
    global _last_beat
    if not configured() or (not force and time.time() - _last_beat < HEARTBEAT_EVERY_S):
        return
    _last_beat = time.time()
    now = datetime.utcnow().replace(microsecond=0)
    t = db.text('UPDATE lake_heartbeat SET beat = :b WHERE id = 1')
    if db.session.execute(t, {'b': now}).rowcount == 0:
        db.session.execute(db.text('INSERT INTO lake_heartbeat (id, beat) VALUES (1, :b)'), {'b': now})
    db.session.commit()


def status():
    """{'configured', 'reachable', 'lag_seconds', 'healthy', 'detail'}"""
    if not configured():
        return {'configured': False, 'reachable': False, 'lag_seconds': None, 'healthy': False,
                'detail': 'No data lake configured (LAKE_DATABASE_URL).'}
    try:
        with engine().connect() as c:
            lake_beat = c.execute(db.text('SELECT beat FROM lake_heartbeat WHERE id = 1')).scalar()
        hot_beat = db.session.execute(db.text('SELECT beat FROM lake_heartbeat WHERE id = 1')).scalar()
    except Exception as e:
        return {'configured': True, 'reachable': False, 'lag_seconds': None, 'healthy': False,
                'detail': f'Data lake not reachable: {type(e).__name__}'}
    if lake_beat is None or hot_beat is None:
        return {'configured': True, 'reachable': True, 'lag_seconds': None, 'healthy': False,
                'detail': 'Waiting for the first heartbeat to replicate.'}
    lake_beat, hot_beat = _dt(lake_beat), _dt(hot_beat)
    lag = max(0, int((hot_beat - lake_beat).total_seconds()))
    stale = (datetime.utcnow() - hot_beat).total_seconds() > HEARTBEAT_EVERY_S * 5
    return {'configured': True, 'reachable': True, 'lag_seconds': lag, 'healthy': lag <= MAX_READ_LAG_S and not stale,
            'detail': f'Replicating, {lag} s behind' if lag <= MAX_READ_LAG_S else f'Lake is {lag} s behind'}


def _dt(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


class _Reader:
    """A session for reads of history: the lake when healthy, else the hot DB."""

    def __init__(self):
        self.session, self.source, self._own = db.session, 'hot', False
        if configured():
            st = status()
            if st['healthy']:
                from sqlalchemy.orm import Session
                self.session, self.source, self._own = Session(bind=engine()), 'lake', True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        if self._own:
            self.session.close()


def reader():
    """with lake.reader() as r: r.session.query(Model)...; r.source is 'lake' or 'hot'."""
    return _Reader()


def purge(table, column, cutoff, batch=20000):
    """Delete rows older than cutoff from the hot database only. Returns the
    number deleted, or None when it was skipped to protect data."""
    if table not in {t for t, _ in RETAINED} or column not in {c for _, c in RETAINED}:
        raise ValueError('not a retained table')
    # Bandit B608 reviewed: identifiers come from a fixed allow-list; values are bound parameters (names from the fixed list above)
    q = db.text(f'DELETE FROM {table} WHERE {column} < :cutoff')   # nosec B608
    if not configured():
        n = db.session.execute(q, {'cutoff': cutoff}).rowcount
        db.session.commit()
        return n
    st = status()
    if not st['healthy'] or (st['lag_seconds'] or 0) > MAX_PURGE_LAG_S:
        current_app.logger.warning('hot purge of %s skipped: %s', table, st['detail'])
        return None
    # The lake must already hold every row we are about to delete. Replication
    # applies changes in order, so the lake holding the newest of them (and at
    # least as many) means it holds them all.
    # Bandit B608 reviewed: identifiers come from a fixed allow-list; values are bound parameters
    count = db.text(f'SELECT COUNT(*), MAX(id) FROM {table} WHERE {column} < :cutoff')  # nosec B608
    hot_n, hot_max = db.session.execute(count, {'cutoff': cutoff}).one()
    db.session.commit()                         # end this read; the delete runs on its own connection
    if not hot_n:
        return 0
    with engine().connect() as c:
        lake_n = c.execute(count, {'cutoff': cutoff}).one()[0] or 0
        # Bandit B608 reviewed: identifiers come from a fixed allow-list; values are bound parameters
        has_newest = c.execute(db.text(f'SELECT 1 FROM {table} WHERE id = :i'), {'i': hot_max}).scalar()  # nosec B608
    if lake_n < hot_n or not has_newest:
        current_app.logger.warning('hot purge of %s skipped: the lake does not hold every row yet', table)
        return None
    total = 0
    mysql = db.engine.dialect.name == 'mysql'
    with db.engine.connect() as conn:
        if mysql:
            conn.execute(db.text('SET SESSION sql_log_bin = 0'))   # the replica never sees this delete
        try:
            while True:
                limit = f' LIMIT {int(batch)}' if mysql else ''
                # Bandit B608 reviewed: identifiers come from a fixed allow-list; values are bound parameters
                n = conn.execute(db.text(f'DELETE FROM {table} WHERE {column} < :cutoff{limit}'),  # nosec B608
                                 {'cutoff': cutoff}).rowcount
                conn.commit()
                total += n
                if not mysql or n < batch:
                    break
        finally:
            if mysql:
                conn.execute(db.text('SET SESSION sql_log_bin = 1'))
                conn.commit()
    db.session.expire_all()                     # later reads in this session see the purge
    return total


def retention_policy():
    import json
    from app.models import Settings
    row = Settings.query.filter_by(key=RETENTION_SETTING).first()
    try:
        p = json.loads(row.value) if row and row.value else {}
    except ValueError:
        p = {}
    enabled = bool(p.get('enabled')) and p.get('period') in PERIODS
    return {'enabled': enabled, 'period': p.get('period') if p.get('period') in PERIODS else 'year'}


def save_retention_policy(enabled, period):
    import json
    from app.models import Settings
    if period not in PERIODS:
        raise ValueError('period')
    row = Settings.query.filter_by(key=RETENTION_SETTING).first()
    value = json.dumps({'enabled': bool(enabled), 'period': period})
    if row:
        row.value = value
    else:
        db.session.add(Settings(key=RETENTION_SETTING, value=value))
    db.session.commit()


def apply_retention():
    """Trim the hot database to the policy (only with a healthy lake)."""
    from app.services.activity_service import log_activity
    p = retention_policy()
    if not p['enabled']:
        return None
    if not configured():
        current_app.logger.warning('hot retention is on but no data lake is configured; nothing deleted')
        return None
    cutoff = datetime.utcnow() - timedelta(days=PERIODS[p['period']])
    done = {}
    for table, column in RETAINED:
        n = purge(table, column, cutoff)
        if n is None:
            return None                                    # stop at the first table the lake cannot confirm
        if n:
            done[table] = n
    if done:
        log_activity('Hot retention applied', status='success',
                     response=f"older than one {p['period']}: " + ', '.join(f'{t} {n}' for t, n in done.items())
                     + ' (kept in the data lake)')
    return done
