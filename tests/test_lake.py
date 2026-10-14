"""Hot database and data lake: reads go to the lake when it is healthy, hot
purges never delete anything the lake has not got, and the hot retention
policy is off by default and only administrators change it. A second SQLite
file stands in for the replica (the real MariaDB replication is tested in CI:
.github/workflows/ci.yml, job "Data lake replication")."""
import os
import tempfile
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app import create_app, db
from app.models import ActivityLog, SystemMetrics
from app.models.user import User


@pytest.fixture
def lake_url():
    import uuid                                  # one file per test (Windows keeps files locked)
    path = os.path.join(tempfile.gettempdir(), f'edgeshell-lake-{os.getpid()}-{uuid.uuid4().hex[:8]}.db')
    return 'sqlite:///' + path.replace('\\', '/')


@pytest.fixture
def app(lake_url):
    import app.services.lake as lake
    lake._engine = None
    app = create_app('testing')
    app.config['LAKE_DATABASE_URL'] = lake_url
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.metadata.create_all(create_engine(lake_url))          # the replica's schema
        from app import _seed_defaults
        _seed_defaults()
        a = User.query.filter_by(username='admin').first()
        a.must_change_password, a.email = False, 'o@fleet.test'
        op = User(username='op', email='op@fleet.test', role='operator', is_active=True, must_change_password=False)
        op.set_password('Operator-pass-1')
        db.session.add(op)
        from app.models.node import Node
        db.session.add(Node(id=1, name='node-1', ip_address='10.0.0.1', username='pi', status='online'))
        db.session.commit()
    yield app
    lake._engine = None


def replicate(app, lake_url, beat_lag_s=0):
    """Copy the hot heartbeat and history rows to the stand-in lake."""
    with app.app_context():
        eng = create_engine(lake_url)
        hot_beat = db.session.execute(text('select beat from lake_heartbeat where id = 1')).scalar()
        rows = db.session.execute(text('select id, node_id, timestamp, status, cpu_percent from system_metrics')).all()
        nodes = db.session.execute(text('select id, name, ip_address, username from nodes')).all()
        with eng.begin() as c:
            for n in nodes:
                if not c.execute(text('select 1 from nodes where id = :i'), {'i': n.id}).scalar():
                    c.execute(text('insert into nodes (id, name, ip_address, username) values (:i, :n, :a, :u)'),
                              {'i': n.id, 'n': n.name, 'a': n.ip_address, 'u': n.username})
            c.execute(text('delete from lake_heartbeat'))
            if hot_beat is not None:
                b = hot_beat if isinstance(hot_beat, datetime) else datetime.fromisoformat(str(hot_beat))
                c.execute(text('insert into lake_heartbeat (id, beat) values (1, :b)'),
                          {'b': b - timedelta(seconds=beat_lag_s)})
            for r in rows:
                if not c.execute(text('select 1 from system_metrics where id = :i'), {'i': r.id}).scalar():
                    c.execute(text('insert into system_metrics (id, node_id, timestamp, status, cpu_percent) '
                                   'values (:i, :n, :t, :s, :c)'),
                              {'i': r.id, 'n': r.node_id, 't': r.timestamp, 's': r.status, 'c': r.cpu_percent})


def old_and_new_samples(app):
    with app.app_context():
        for days in (40, 39, 1):
            db.session.add(SystemMetrics(node_id=1, timestamp=datetime.utcnow() - timedelta(days=days),
                                         status='online', cpu_percent=days))
        db.session.commit()


def test_reads_use_the_lake_only_when_it_is_healthy(app, lake_url):
    from app.services import lake
    with app.app_context():
        lake.beat(force=True)
        with lake.reader() as r:
            assert r.source == 'hot'                       # nothing replicated yet
    replicate(app, lake_url)
    with app.app_context():
        st = lake.status()
        assert st['healthy'] and st['lag_seconds'] == 0
        with lake.reader() as r:
            assert r.source == 'lake'
    replicate(app, lake_url, beat_lag_s=3600)              # an hour behind
    with app.app_context():
        assert not lake.status()['healthy']
        with lake.reader() as r:
            assert r.source == 'hot'
    app.config['LAKE_DATABASE_URL'] = 'sqlite:////nonexistent/dir/lake.db'
    lake._engine = None
    with app.app_context():
        st = lake.status()
        assert not st['reachable']
        with lake.reader() as r:
            assert r.source == 'hot'


def test_without_a_lake_purges_delete_as_before(app):
    from app.services import lake
    app.config['LAKE_DATABASE_URL'] = ''
    old_and_new_samples(app)
    with app.app_context():
        assert lake.purge('system_metrics', 'timestamp', datetime.utcnow() - timedelta(days=14)) == 2
        assert SystemMetrics.query.count() == 1


def test_a_purge_never_deletes_what_the_lake_has_not_got(app, lake_url):
    from app.services import lake
    old_and_new_samples(app)
    with app.app_context():
        lake.beat(force=True)
    cutoff = datetime.utcnow() - timedelta(days=14)
    with app.app_context():                                # lake not replicating yet: skipped
        assert lake.purge('system_metrics', 'timestamp', cutoff) is None
        assert SystemMetrics.query.count() == 3
    replicate(app, lake_url, beat_lag_s=600)               # behind: skipped
    with app.app_context():
        assert lake.purge('system_metrics', 'timestamp', cutoff) is None
    replicate(app, lake_url)
    with create_engine(lake_url).begin() as c:             # the lake is missing a row: skipped
        c.execute(text('delete from system_metrics where cpu_percent = 40'))
    with app.app_context():
        assert lake.purge('system_metrics', 'timestamp', cutoff) is None
        assert SystemMetrics.query.count() == 3
    with create_engine(lake_url).begin() as c:
        c.execute(text("insert into system_metrics (node_id, timestamp, status, cpu_percent) "
                       "values (1, :t, 'online', 40)"), {'t': datetime.utcnow() - timedelta(days=40)})
    with app.app_context():                                # healthy and complete: hot trimmed, lake kept
        assert lake.purge('system_metrics', 'timestamp', cutoff) == 2
        assert SystemMetrics.query.count() == 1
    with create_engine(lake_url).connect() as c:
        assert c.execute(text('select count(*) from system_metrics')).scalar() == 3


def test_only_fixed_tables_can_be_purged(app):
    from app.services import lake
    with app.app_context():
        with pytest.raises(ValueError):
            lake.purge('users', 'created_at', datetime.utcnow())
        with pytest.raises(ValueError):
            lake.purge('system_metrics; drop table users', 'timestamp', datetime.utcnow())


def test_hot_retention_is_off_by_default_and_admin_only(app, lake_url):
    from app.services import lake
    with app.app_context():
        assert lake.retention_policy() == {'enabled': False, 'period': 'year'}
        assert lake.apply_retention() is None
    op = app.test_client()
    op.post('/login', data={'username': 'op', 'password': 'Operator-pass-1'})
    op.post('/settings/', data={'section': 'hot_retention', 'enabled': 'on', 'period': 'month'})
    with app.app_context():
        assert not lake.retention_policy()['enabled']                 # operators cannot
    adm = app.test_client()
    adm.post('/login', data={'username': 'admin', 'password': 'admin123'})
    r = adm.post('/settings/', data={'section': 'hot_retention', 'enabled': 'on', 'period': 'month'})
    assert r.status_code in (200, 302)
    with app.app_context():
        assert lake.retention_policy() == {'enabled': True, 'period': 'month'}
        assert ActivityLog.query.filter(ActivityLog.action.like('Hot retention%')).count() == 1
    adm.post('/settings/', data={'section': 'hot_retention', 'period': 'decade'})
    with app.app_context():
        assert lake.retention_policy()['period'] == 'month'           # invalid period refused


def test_retention_trims_only_with_a_healthy_complete_lake(app, lake_url):
    from app.services import lake
    old_and_new_samples(app)
    with app.app_context():
        lake.save_retention_policy(True, 'month')
        lake.beat(force=True)
        assert lake.apply_retention() is None                         # lake not ready: nothing deleted
        assert SystemMetrics.query.count() == 3
    replicate(app, lake_url)
    with app.app_context():
        done = lake.apply_retention()
        assert done == {'system_metrics': 2} and SystemMetrics.query.count() == 1
    with create_engine(lake_url).connect() as c:
        assert c.execute(text('select count(*) from system_metrics')).scalar() == 3


def test_history_reads_report_their_source(app, lake_url):
    from app.models.node import Node
    from app.services import lake
    with app.app_context():
        nid = Node.query.first().id
        lake.beat(force=True)
    c = app.test_client()
    tok = c.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'admin123'}).get_json()['token']
    h = {'Authorization': f'Bearer {tok}'}
    assert c.get(f'/api/v1/metrics/history?node_id={nid}', headers=h).get_json()['source'] == 'hot'
    replicate(app, lake_url)
    with create_engine(lake_url).begin() as conn:                    # the lake also needs the activity table rows
        pass
    assert c.get(f'/api/v1/metrics/history?node_id={nid}', headers=h).get_json()['source'] == 'lake'
    assert c.get('/api/v1/activity', headers=h).get_json()['source'] == 'lake'
