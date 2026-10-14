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

        # App deletes reach the hot database only; names can be reused safely.
        from app.models.user import User
        from app.routes.nodes import _purge_nodes
        from app.routes.users import _delete_user_row
        doomed = Node(name='reused-name', ip_address='10.0.0.9', username='pi', status='offline')
        db.session.add(doomed)
        u = User(username='leaver', email='leaver@example.org', role='monitor', is_active=True,
                 must_change_password=False)
        u.set_password('Monitor-pass-1')
        db.session.add(u)
        db.session.commit()
        doomed_id, uid = doomed.id, u.id
        db.session.add(SystemMetrics(node_id=doomed_id, timestamp=datetime.utcnow(), status='offline'))
        db.session.commit()
        wait(lambda: lake_count('select count(*) from system_metrics where node_id = :i', i=doomed_id) == 1
             and lake_count('select count(*) from users where id = :i', i=uid) == 1, label='new rows')
        lake.beat(force=True)
        wait(lambda: lake.status()['healthy'], label='heartbeat')
        with app.test_request_context():
            _purge_nodes([db.session.get(Node, doomed_id)])
            _delete_user_row(db.session.get(User, uid))
        check('a deleted node and user leave the hot database',
              db.session.get(Node, doomed_id) is None and db.session.get(User, uid) is None)
        time.sleep(3)
        check('the lake keeps the deleted node, its history and the user',
              lake_count('select count(*) from nodes where id = :i', i=doomed_id) == 1
              and lake_count('select count(*) from system_metrics where node_id = :i', i=doomed_id) == 1
              and lake_count('select count(*) from users where id = :i', i=uid) == 1)
        check('the kept rows were renamed so the names are free again',
              lake_count("select count(*) from nodes where name = 'reused-name'") == 0
              and lake_count("select count(*) from users where username = 'leaver'") == 0)
        db.session.add(Node(name='reused-name', ip_address='10.0.0.10', username='pi', status='offline'))
        u2 = User(username='leaver', email='leaver@example.org', role='monitor', is_active=True,
                  must_change_password=False)
        u2.set_password('Monitor-pass-1')
        db.session.add(u2)
        db.session.commit()
        check('re-creating a node and a user with the same names keeps replication running',
              wait(lambda: lake_count("select count(*) from nodes where name = 'reused-name'") == 1
                   and lake_count("select count(*) from users where username = 'leaver'") == 1, label='reuse'))
        # A full-width name, deleted and reused twice: the suffix must survive
        # or the second tombstone collides on the lake and stops replication.
        long = 'L' * 64
        for round_ in (1, 2):
            ln = Node(name=long, ip_address=f'10.0.1.{round_}', username='pi', status='offline')
            db.session.add(ln)
            db.session.commit()
            wait(lambda: lake_count('select count(*) from nodes where name = :n', n=long) == 1, label='long name')
            lake.beat(force=True)
            wait(lambda: lake.status()['healthy'], label='heartbeat')
            with app.test_request_context():
                _purge_nodes([ln])
        db.session.add(Node(name=long, ip_address='10.0.1.3', username='pi', status='offline'))
        db.session.commit()
        check('a 64-character name can be deleted and reused again and again',
              wait(lambda: lake_count('select count(*) from nodes where name = :n', n=long) == 1
                   and lake_count("select count(*) from nodes where name like '%~deleted-%'") >= 3, label='reuse long'))

        # Housekeeping of sessions and expired credentials leaves the lake alone.
        from app.models.user import UserSession
        lake.beat(force=True)
        wait(lambda: lake.status()['healthy'], label='heartbeat')
        old = datetime.utcnow() - timedelta(days=60)
        s_row = UserSession(user_id=u2.id, token='t' * 64, ip='10.0.0.1', agent='test', started_at=old,
                            seen_at=old, active_at=old)
        db.session.add(s_row)
        db.session.commit()
        sid = s_row.id
        wait(lambda: lake_count('select count(*) from user_sessions where id = :i', i=sid) == 1, label='session')
        lake.beat(force=True)
        wait(lambda: lake.status()['healthy'], label='heartbeat')
        from app.services.collector import COLLECTOR
        COLLECTOR._purge_history()
        time.sleep(3)
        check('old sessions leave the hot database but stay in the lake',
              db.session.get(UserSession, sid) is None
              and lake_count('select count(*) from user_sessions where id = :i', i=sid) == 1)

        before = lake_count('select count(*) from activity_logs')
        lake.beat(force=True)
        wait(lambda: lake.status()['healthy'], label='heartbeat')
        lake.hot_only([('DELETE FROM activity_logs', {})])
        time.sleep(3)
        check('clearing the activity log keeps it in the lake',
              ActivityLog.query.count() == 0 and lake_count('select count(*) from activity_logs') >= before > 0)
        try:
            with lake_eng.begin() as c:
                c.execute(text("insert into settings (`key`, value) values ('x-from-app', 'y')"))
            wrote = True
        except Exception:
            wrote = False
        check('the app account cannot change the lake', not wrote)

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
