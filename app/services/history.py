"""Long-term node history: samples, hourly rollups and the stats export.

The collector writes one sample per active node every minute into
system_metrics, whatever the node's state (online, offline, login failed),
so uptime can be measured, not guessed. Samples are kept for
METRICS_RETENTION_DAYS. Every hour they are rolled up into metrics_hourly
(one row per node per hour), which is kept for good: uptime, resource use and
reboots over months and years, at about 1.3 million rows a year for 150 nodes.

The export reads that history in a background job and writes a CSV, one row
per node per day (or hour, or for the whole period), tagged with the node's
category so a separate pipeline can train health-prediction models on it.
"""
import csv
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta

from app import db

PERIODS = {                     # name -> (label, days)
    'day': ('Last 24 hours', 1),
    'week': ('Last 7 days', 7),
    'month': ('Last 30 days', 30),
    'quarter': ('Last quarter (90 days)', 90),
    'semester': ('Last semester (182 days)', 182),
    'year': ('Last year (365 days)', 365),
}
GRAINS = {
    'daily': 'One row per node per day',
    'hourly': 'One row per node per hour',
    'summary': 'One row per node for the whole period',
}
EXPORT_DIR = os.path.join(tempfile.gettempdir(), 'edgeshell-exports')
EXPORT_KEEP_S = 3600
ROLLED_KEY = 'history_rolled_until'
_ROLL_LOCK = threading.Lock()           # the collector and an export may roll up at once

COLUMNS = ['period_start_utc', 'period_end_utc', 'node_id', 'node_name', 'category', 'node_type',
           'location', 'environment', 'ip_address', 'samples', 'uptime_percent', 'online_samples',
           'offline_samples', 'login_failed_samples', 'cpu_avg', 'cpu_max', 'ram_avg', 'ram_max',
           'disk_avg', 'disk_max', 'temp_avg', 'temp_max', 'load1_avg', 'load1_max',
           'uptime_max_s', 'reboots', 'net_rx_bytes', 'net_tx_bytes']


def hour_floor(t):
    return t.replace(minute=0, second=0, microsecond=0)


def sample_rows(nodes, latest, fresh_s):
    """System_metrics rows for one sampling tick: every node with its state,
    and its metrics when the last collection succeeded within fresh_s."""
    from app.models import SystemMetrics
    from app.services import heartbeat_service
    now = datetime.utcnow()
    rows = []
    for n in nodes:
        r = latest.get(n.id)
        ok = bool(r and r.get('ok') and (now - r['at']).total_seconds() <= fresh_s)
        m = r['metrics'] if ok else {}
        status = 'online' if ok else (n.status if n.status in ('offline', 'auth_failed') else 'offline')
        rows.append(SystemMetrics(
            node_id=n.id, timestamp=now, status=status, heartbeat=heartbeat_service.fresh(n, now),
            cpu_percent=m.get('cpu_percent'), ram_percent=m.get('mem_percent'),
            disk_percent=m.get('disk_percent'), temperature=m.get('temperature'),
            load_avg_1=m.get('load_1'), load_avg_5=m.get('load_5'), load_avg_15=m.get('load_15'),
            uptime_seconds=m.get('uptime_seconds'), network_rx=m.get('net_rx_bytes'),
            network_tx=m.get('net_tx_bytes'),
            docker_running=m.get('docker_installed') if ok else None,
            nodered_running=m.get('nodered_installed') if ok else None))
    return rows


def _setting(key, value=None):
    from app.models import Settings
    row = Settings.query.filter_by(key=key).first()
    if value is None:
        return row.value if row else None
    if row:
        row.value = value
    else:
        db.session.add(Settings(key=key, value=value))


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _max(xs):
    xs = [x for x in xs if x is not None]
    return round(max(xs), 2) if xs else None


def _rollup_hour(start):
    """Aggregate one hour of samples into metrics_hourly (replacing any row)."""
    from app.models import MetricsHourly, SystemMetrics
    end = start + timedelta(hours=1)
    rows = (db.session.query(SystemMetrics.node_id, SystemMetrics.timestamp, SystemMetrics.status,
                             SystemMetrics.cpu_percent, SystemMetrics.ram_percent,
                             SystemMetrics.disk_percent, SystemMetrics.temperature,
                             SystemMetrics.load_avg_1, SystemMetrics.uptime_seconds,
                             SystemMetrics.network_rx, SystemMetrics.network_tx, SystemMetrics.heartbeat)
            .filter(SystemMetrics.timestamp >= start, SystemMetrics.timestamp < end)
            .order_by(SystemMetrics.node_id, SystemMetrics.timestamp).all())
    by_node = {}
    for r in rows:
        by_node.setdefault(r.node_id, []).append(r)
    MetricsHourly.query.filter(MetricsHourly.hour == start).delete(synchronize_session=False)
    for nid, rs in by_node.items():
        if nid is None:
            continue
        # Up when SSH reached the node OR a verified heartbeat arrived: a VPN
        # gap with heartbeats flowing is not node downtime (ADR 0013).
        st = [('online' if (r.status or 'online') == 'online' or r.heartbeat else (r.status or 'online')) for r in rs]
        hb_only = sum(1 for r in rs if r.heartbeat and (r.status or 'online') != 'online')
        ups = [r.uptime_seconds for r in rs if r.uptime_seconds is not None]
        reboots = sum(1 for a, b in zip(ups, ups[1:]) if b < a)
        rx = [r.network_rx for r in rs if r.network_rx is not None]
        tx = [r.network_tx for r in rs if r.network_tx is not None]
        db.session.add(MetricsHourly(
            node_id=nid, hour=start, samples=len(rs), online=st.count('online'), heartbeat_only=hb_only,
            offline=st.count('offline'), auth_failed=st.count('auth_failed'),
            cpu_avg=_avg([r.cpu_percent for r in rs]), cpu_max=_max([r.cpu_percent for r in rs]),
            ram_avg=_avg([r.ram_percent for r in rs]), ram_max=_max([r.ram_percent for r in rs]),
            disk_avg=_avg([r.disk_percent for r in rs]), disk_max=_max([r.disk_percent for r in rs]),
            temp_avg=_avg([r.temperature for r in rs]), temp_max=_max([r.temperature for r in rs]),
            load1_avg=_avg([r.load_avg_1 for r in rs]), load1_max=_max([r.load_avg_1 for r in rs]),
            uptime_max_s=max(ups) if ups else None, reboots=reboots,
            # counters reset on reboot; count only increases
            net_rx_bytes=sum(max(0, b - a) for a, b in zip(rx, rx[1:])) if len(rx) > 1 else None,
            net_tx_bytes=sum(max(0, b - a) for a, b in zip(tx, tx[1:])) if len(tx) > 1 else None))


def roll_up(until=None, on_hour=None, max_hours=None):
    """Roll up every complete hour not rolled up yet (up to `until`, default
    the current hour). Starts from the oldest sample on first use. Returns
    the number of hours processed."""
    with _ROLL_LOCK:
        return _roll_up(hour_floor(until or datetime.utcnow()), on_hour, max_hours)


def _roll_up(until, on_hour, max_hours):
    from app.models import SystemMetrics
    done = _setting(ROLLED_KEY)
    if done:
        start = datetime.fromisoformat(done)
    else:
        first = db.session.query(db.func.min(SystemMetrics.timestamp)).scalar()
        if not first:
            _setting(ROLLED_KEY, until.isoformat())
            db.session.commit()
            return 0
        start = hour_floor(first)
    hours = 0
    total = max(0, int((until - start).total_seconds() // 3600))
    while start < until and (not max_hours or hours < max_hours):
        _rollup_hour(start)
        start += timedelta(hours=1)
        _setting(ROLLED_KEY, start.isoformat())
        db.session.commit()
        hours += 1
        if on_hour:
            on_hour(hours, total)
    return hours


def _merge(rows):
    """Combine several hourly rows of one node into one period row."""
    w = lambda f: [(getattr(r, f), r.samples - r.offline - r.auth_failed) for r in rows]

    def wavg(f):
        pairs = [(v, n) for v, n in w(f) if v is not None and n > 0]
        tot = sum(n for _, n in pairs)
        return round(sum(v * n for v, n in pairs) / tot, 2) if tot else None

    def mx(f):
        vals = [getattr(r, f) for r in rows if getattr(r, f) is not None]
        return max(vals) if vals else None

    def total(f):
        vals = [getattr(r, f) for r in rows if getattr(r, f) is not None]
        return sum(vals) if vals else None
    samples = sum(r.samples for r in rows)
    online = sum(r.online for r in rows)
    return {
        'samples': samples, 'online_samples': online,
        'offline_samples': sum(r.offline for r in rows),
        'login_failed_samples': sum(r.auth_failed for r in rows),
        'uptime_percent': round(online / samples * 100, 2) if samples else None,
        'cpu_avg': wavg('cpu_avg'), 'cpu_max': mx('cpu_max'),
        'ram_avg': wavg('ram_avg'), 'ram_max': mx('ram_max'),
        'disk_avg': wavg('disk_avg'), 'disk_max': mx('disk_max'),
        'temp_avg': wavg('temp_avg'), 'temp_max': mx('temp_max'),
        'load1_avg': wavg('load1_avg'), 'load1_max': mx('load1_max'),
        'uptime_max_s': mx('uptime_max_s'), 'reboots': total('reboots') or 0,
        'net_rx_bytes': total('net_rx_bytes'), 'net_tx_bytes': total('net_tx_bytes'),
    }


def _bucket(hour, grain, start):
    if grain == 'hourly':
        return hour, hour + timedelta(hours=1)
    if grain == 'daily':
        d = hour.replace(hour=0)
        return d, d + timedelta(days=1)
    return start, None


def export_task(task, period, grain, category, owner_id):
    """Background job: roll up, then write the CSV and attach its link."""
    from app.models import MetricsHourly
    from app.models.node import Node
    from app.services import categories as cats
    label, days = PERIODS[period]
    now = datetime.utcnow()
    end = hour_floor(now) + timedelta(hours=1)
    start = hour_floor(now - timedelta(days=days))
    task.progress(0, 100, 'Bringing the hourly history up to date')
    task.log(f'{label}, {GRAINS[grain].lower()}, category: {category or "all"}')
    roll_up(until=now)
    with _ROLL_LOCK:
        _rollup_hour(hour_floor(now))          # include the hour in progress
        db.session.commit()

    categories = cats.load()
    nodes = Node.query.filter(Node.is_deleted.isnot(True)).order_by(Node.name).all()
    if category:
        nodes = [n for n in nodes if cats.category_of(n.node_type, categories) == category]
    task.progress(5, 100, f'Reading the history of {len(nodes)} node(s)')
    os.makedirs(EXPORT_DIR, exist_ok=True)
    _prune_exports()
    stamp = now.strftime('%Y%m%d-%H%M')
    slug = (category or 'all').lower().replace(' ', '-')
    fname = f'edgeshell-stats-{period}-{grain}-{slug}-{stamp}.csv'
    fd, path = tempfile.mkstemp(prefix=f'{owner_id}-', suffix='.csv', dir=EXPORT_DIR)
    rows_out = 0
    from app.services import lake
    reader = lake.reader()                     # the data lake when healthy, else the hot DB
    task.log(f'Reading from the {"data lake" if reader.source == "lake" else "hot database"}')
    with reader, os.fdopen(fd, 'w', newline='', encoding='utf-8') as f:
        out = csv.writer(f)
        out.writerow(COLUMNS)
        for i, n in enumerate(nodes, 1):
            hourly = (reader.session.query(MetricsHourly).filter(MetricsHourly.node_id == n.id,
                                                 MetricsHourly.hour >= start, MetricsHourly.hour < end)
                      .order_by(MetricsHourly.hour).all())
            groups = {}
            for h in hourly:
                groups.setdefault(_bucket(h.hour, grain, start), []).append(h)
            for (b0, b1), rs in sorted(groups.items()):
                merged = _merge(rs)
                row = {'period_start_utc': b0.isoformat() + 'Z',
                       'period_end_utc': (b1 or end).isoformat() + 'Z',
                       'node_id': n.id, 'node_name': n.name,
                       'category': cats.category_of(n.node_type, categories),
                       'node_type': n.node_type or '', 'location': n.location or '',
                       'environment': n.environment or '', 'ip_address': n.ip_address, **merged}
                out.writerow(['' if row[c] is None else row[c] for c in COLUMNS])
                rows_out += 1
            if i % 5 == 0 or i == len(nodes):
                task.progress(5 + int(95 * i / max(1, len(nodes))), 100,
                              f'{i} of {len(nodes)} node(s), {rows_out} row(s)')
    token = os.path.basename(path)
    task.result(download=token, filename=fname, rows=rows_out, nodes=len(nodes),
                size=os.path.getsize(path))
    task.progress(100, 100, f'{rows_out} row(s) for {len(nodes)} node(s) ready to download')
    task.log(f'Wrote {rows_out} row(s), {os.path.getsize(path)} bytes')


def export_path(token, owner_id):
    """The file of a finished export, only for the user who made it."""
    name = os.path.basename(token or '')
    if not name.startswith(f'{owner_id}-') or not name.endswith('.csv'):
        return None
    path = os.path.join(EXPORT_DIR, name)
    return path if os.path.isfile(path) else None


def _prune_exports():
    cutoff = time.time() - EXPORT_KEEP_S
    for name in os.listdir(EXPORT_DIR):
        p = os.path.join(EXPORT_DIR, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass
