"""End-to-end data lake test against two real MariaDB servers (CI job "Data
lake replication"): the hot database replicates to the lake, a hot purge does
not reach the lake, replication keeps flowing afterwards, the hot retention
policy trims only the hot database, and purges refuse to run while
replication is stopped.

    TEST_DATABASE_URL=mysql+mysqlconnector://edgeshell:<pw>@127.0.0.1:3306/edgeshell
    LAKE_DATABASE_URL=mysql+mysqlconnector://edgeshell:<pw>@127.0.0.1:3307/edgeshell
    LAKE_ROOT_URL=mysql+mysqlconnector://root:<pw>@127.0.0.1:3307/
    python tools/lake_e2e.py [--schema-only]
"""
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('FLASK_ENV', 'testing')

from sqlalchemy import create_engine, text  # noqa: E402

from app import create_app, db  # noqa: E402


def check(label, ok, detail=''):
    print(('  PASS  ' if ok else '  FAIL  ') + label + (f' ({detail})' if detail and not ok else ''))
    if not ok:
        sys.exit(1)


def wait(cond, seconds=90, label=''):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if cond():
            return True
        time.sleep(1)
    print('  timed out waiting for', label)
    return False


def main():
    app = create_app('testing')
    if '--schema-only' in sys.argv:
        print('schema created on the hot database')
        return
    app.config['LAKE_DATABASE_URL'] = os.environ['LAKE_DATABASE_URL']
    lake_eng = create_engine(os.environ['LAKE_DATABASE_URL'])
    root = create_engine(os.environ['LAKE_ROOT_URL'])

    def lake_count(sql, **p):
        with lake_eng.connect() as c:
            return c.execute(text(sql), p).scalar()

    from app.models import ActivityLog, SystemMetrics
    from app.models.node import Node
    from app.services import lake
    with app.app_context():
        n = Node(name='lake-node', ip_address='10.0.0.1', username='pi', status='online')
        db.session.add(n)
        db.session.commit()
        for days in (40, 39, 1):
            db.session.add(SystemMetrics(node_id=n.id, timestamp=datetime.utcnow() - timedelta(days=days),
                                         status='online', cpu_percent=days))
        db.session.commit()
        lake.beat(force=True)
        check('hot writes replicate to the lake',
              wait(lambda: lake_count('select count(*) from system_metrics') == 3, label='replication'))
        check('replication health is measured end to end', wait(lambda: lake.status()['healthy'], label='heartbeat'),
              lake.status()['detail'])
        with lake.reader() as r:
            check('history reads go to the lake', r.source == 'lake', r.source)

        n_deleted = lake.purge('system_metrics', 'timestamp', datetime.utcnow() - timedelta(days=14))
        check('a hot purge trims the hot database', n_deleted == 2 and SystemMetrics.query.count() == 1,
              f'deleted {n_deleted}')
        time.sleep(3)
        check('the lake keeps every purged row', lake_count('select count(*) from system_metrics') == 3)

        db.session.add(SystemMetrics(node_id=n.id, timestamp=datetime.utcnow(), status='online', cpu_percent=5))
        db.session.commit()
        check('replication keeps flowing after a purge',
              wait(lambda: lake_count('select count(*) from system_metrics') == 4, label='new row'))

        db.session.add(ActivityLog(action='old entry', status='success',
                                   timestamp=datetime.utcnow() - timedelta(days=10)))
        db.session.commit()
        wait(lambda: lake_count("select count(*) from activity_logs where action = 'old entry'") == 1, label='log')
        lake.save_retention_policy(True, 'week')
        lake.beat(force=True)
        wait(lambda: lake.status()['healthy'], label='heartbeat')
        done = lake.apply_retention()
        check('hot retention trims the hot database', bool(done) and done.get('activity_logs') == 1, str(done))
        time.sleep(3)
        check('hot retention leaves the lake complete',
              lake_count("select count(*) from activity_logs where action = 'old entry'") == 1)

        with root.connect() as c:
            c.execute(text('STOP REPLICA'))
        db.session.add(SystemMetrics(node_id=n.id, timestamp=datetime.utcnow() - timedelta(days=30),
                                     status='online', cpu_percent=30))
        db.session.commit()
        lake.MAX_PURGE_LAG_S = 1
        time.sleep(2)
        lake.beat(force=True)
        time.sleep(2)
        r = lake.purge('system_metrics', 'timestamp', datetime.utcnow() - timedelta(days=14))
        check('purges refuse to run while replication is stopped',
              r is None and SystemMetrics.query.filter(SystemMetrics.cpu_percent == 30).count() == 1, str(r))
        with root.connect() as c:
            c.execute(text('START REPLICA'))
        check('replication catches up after it restarts',
              wait(lambda: lake_count('select count(*) from system_metrics where cpu_percent = 30') == 1, label='catch up'))
    print('data lake end-to-end: all checks passed')


if __name__ == '__main__':
    main()
