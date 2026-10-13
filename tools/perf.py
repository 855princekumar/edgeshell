"""Performance check for every page, API and node action.

Runs EdgeShell in-process against a temporary SQLite database (or
TEST_DATABASE_URL) with a large fleet, times each endpoint over several runs
and prints median and 95th percentile. SSH is simulated with a fixed latency
per node (--ssh-ms, default 300, roughly one round trip to a Pi over a VPN),
so the numbers show EdgeShell's own cost plus realistic network waits.

    FLASK_ENV=testing python tools/perf.py --nodes 500 --runs 20

Exit code 1 if any read endpoint's median is above --budget-ms (default 300).
"""
import argparse
import io
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('FLASK_ENV', 'testing')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--nodes', type=int, default=500)
    ap.add_argument('--runs', type=int, default=20)
    ap.add_argument('--ssh-ms', type=int, default=300)
    ap.add_argument('--budget-ms', type=int, default=300)
    args = ap.parse_args()

    from app import create_app, db
    from app.models.node import Node
    from app.models.user import User
    import app.services.collector as collector

    sample = open(os.path.join(os.path.dirname(__file__), 'sample_proc.txt')).read()

    def fake_ssh(t):
        time.sleep(args.ssh_ms / 1000)
        return {'ok': True, 'metrics': collector.parse_metrics(sample), 'kind': None,
                'message': None, 'seconds': args.ssh_ms / 1000, 'reconnected': False}
    collector.collect_one = fake_ssh

    app = create_app('testing')
    with app.app_context():
        db.drop_all()
        db.create_all()
        from app import _seed_defaults
        _seed_defaults()
        admin = User.query.filter_by(username='admin').first()
        admin.must_change_password = False
        admin.email = 'perf@fleet.test'
        for i in range(args.nodes):
            n = Node(name=f'perf-node-{i:04d}', ip_address=f'10.{i // 250}.{i % 250}.1',
                     username='pi', status='online', cpu_percent=12.5, ram_percent=40.1,
                     disk_percent=33.0, load_average='0.5 0.4 0.3', tags='64bit')
            n.password = 'raspberry'
            db.session.add(n)
        db.session.commit()
        first_id = Node.query.first().id

    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    J = {'Accept': 'application/json'}

    def timed(fn, runs):
        out = []
        for _ in range(runs):
            t = time.perf_counter()
            r = fn()
            out.append((time.perf_counter() - t) * 1000)
            assert r.status_code < 500, r.status_code
        return out

    reads = [
        ('GET  /login (signed out)', lambda: app.test_client().get('/login')),
        ('GET  / dashboard', lambda: c.get('/')),
        ('GET  /nodes/ fleet page', lambda: c.get('/nodes/')),
        ('GET  /monitoring/ page', lambda: c.get('/monitoring/')),
        ('GET  /terminal/ page', lambda: c.get('/terminal/')),
        ('GET  /logs/activity', lambda: c.get('/logs/activity')),
        ('GET  /users/', lambda: c.get('/users/')),
        ('GET  /account/', lambda: c.get('/account/')),
        ('GET  /monitoring/fleet-summary (JSON)', lambda: c.get('/monitoring/fleet-summary', headers=J)),
        ('POST /monitoring/poll cached, all nodes', lambda: c.post('/monitoring/poll', json={'cached': True})),
        ('GET  /api/v1/nodes', lambda: c.get('/api/v1/nodes', headers=J)),
        ('GET  /api/v1/status', lambda: c.get('/api/v1/status', headers=J)),
        ('GET  /session/status (polled)', lambda: c.get('/session/status', headers=J)),
        ('GET  /nodes/jobs (polled)', lambda: c.get('/nodes/jobs', headers=J)),
        ('GET  /healthz', lambda: c.get('/healthz')),
    ]
    actions = [
        ('POST /nodes/<id>/test (1 node)', lambda: c.post(f'/nodes/{first_id}/test')),
        ('POST /nodes/<id>/credentials (save + test)',
         lambda: c.post(f'/nodes/{first_id}/credentials', json={'username': 'pi', 'ssh_port': 22})),
        ('POST /monitoring/poll live, 1 node', lambda: c.post('/monitoring/poll', json={'node_ids': [first_id]})),
    ]

    print(f'EdgeShell performance: {args.nodes} nodes, {args.runs} runs each, '
          f'simulated SSH {args.ssh_ms} ms per node\n')
    print(f'{"endpoint":48s} {"median":>9s} {"p95":>9s}')
    slow = []
    for name, fn in reads:
        fn()                                           # warm caches and templates
        t = timed(fn, args.runs)
        med, p95 = statistics.median(t), sorted(t)[int(len(t) * 0.95) - 1]
        flag = '  over budget' if med > args.budget_ms else ''
        if flag:
            slow.append(name)
        print(f'{name:48s} {med:7.1f}ms {p95:7.1f}ms{flag}')
    print()
    for name, fn in actions:
        t = timed(fn, max(3, args.runs // 4))
        print(f'{name:48s} {statistics.median(t):7.1f}ms {max(t):7.1f}ms')

    # Whole-fleet operations
    with app.app_context():
        t = time.perf_counter()
        collector.Collector().run_cycle(force=True)
        fleet = time.perf_counter() - t
    seq = args.nodes * args.ssh_ms / 1000
    print(f'{"collector pass, whole fleet":48s} {fleet * 1000:7.0f}ms  (one by one would take {seq:.0f}s)')

    csv = 'name,ip,username,password\n' + '\n'.join(
        f'import-{i:04d},10.200.{i // 250}.{i % 250 + 1},pi,raspberry' for i in range(args.nodes))
    t = time.perf_counter()
    c.post('/nodes/import', data={'file': (io.BytesIO(csv.encode()), 'fleet.csv')},
               content_type='multipart/form-data')
    upload = time.perf_counter() - t
    t = time.perf_counter()
    c.post('/nodes/import/commit', data={'map__name': 'name', 'map__ip': 'ip_address',
                                             'map__username': 'username', 'map__password': 'password'})
    commit = time.perf_counter() - t
    print(f'{"bulk import upload + preview, " + str(args.nodes) + " rows":48s} {upload * 1000:7.0f}ms')
    print(f'{"bulk import commit (SSH check continues async)":48s} {commit * 1000:7.0f}ms')
    from app.services import jobs
    with app.app_context():
        uid = User.query.filter_by(username='admin').first().id
    t = time.perf_counter()
    while any(j['state'] == 'running' for j in jobs.for_user(uid)):
        time.sleep(0.2)
    print(f'{"background SSH check of the imported nodes":48s} {(time.perf_counter() - t + commit) * 1000:7.0f}ms')
    if slow:
        print(f'\nOver the {args.budget_ms} ms budget: {", ".join(slow)}')
        return 1
    print(f'\nAll read endpoints under the {args.budget_ms} ms budget.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
