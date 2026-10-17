"""Hardware lifecycle (ADR 0015): the identity read over SSH, inventory and
change detection (which part was replaced), confirming or rejecting a change,
fault reports (monitors report, operators resolve), node replacement with
lineage, the fleet numbers, the API with its `report` permission, and signed
webhooks that never call back into the server's own networks."""
import hashlib
import hmac
import json
import socket
from datetime import datetime, timedelta

import pytest

from app import create_app, db, limiter
from app.models import SystemMetrics
from app.models.api import NodeEvent
from app.models.hardware import (HardwareChange, HardwareComponent, HardwareEvent, HardwareFault, Webhook,
                                 WebhookDelivery)
from app.models.node import Node
from app.models.user import User
from app.services import hardware_service as hw
from app.services import hw_identity, webhook_service

PI_OUTPUT = ('model=Raspberry Pi 4 Model B Rev 1.4\nboard=10000000a1b2c3d4\n'
             'storage=9f544953443634478000000000000000\nstorage_model=SD64G 63864569856\niface=eth0\n'
             'network=DC:A6:32:01:02:03\nos=0123456789abcdef0123456789abcdef\n'
             'os_name="Debian GNU/Linux 12 (bookworm)"\nthrottled=0x0\n')


def HW(board='board-1', storage='card-1', network='dc:a6:32:00:00:01', os_id='machine-1', throttled=None,
       model='Raspberry Pi 4 Model B'):
    return {'model': model, 'board': board, 'storage': storage, 'storage_model': 'SD64G 64 GB',
            'network': network, 'iface': 'eth0', 'os': os_id, 'os_name': 'Debian GNU/Linux 12', 'throttled': throttled}


@pytest.fixture
def app():
    app = create_app('testing')
    app.config['WEBHOOK_SYNC'] = True
    limiter.reset()
    with app.app_context():
        db.drop_all()
        db.create_all()
        from app import _seed_defaults
        _seed_defaults()
        a = User.query.filter_by(username='admin').first()
        a.must_change_password, a.email = False, 'owner@fleet.test'
        for name, role in (('op', 'operator'), ('mon', 'monitor')):
            u = User(username=name, email=f'{name}@fleet.test', role=role, is_active=True, must_change_password=False)
            u.set_password('Operator-pass-1')
            db.session.add(u)
        old = datetime.utcnow() - timedelta(days=200)
        for name, loc, status in (('pump-house-2', 'North site', 'online'), ('pump-house-3', 'North site', 'online'),
                                  ('gate-1', 'South site', 'offline')):
            n = Node(name=name, ip_address='10.0.0.1', username='pi', status=status, location=loc,
                     node_type='edge', created_at=old)
            n.password = 'node-secret'
            db.session.add(n)
        db.session.commit()
    yield app


def node(name):
    return Node.query.filter_by(name=name).first()


def browser(app, user='admin'):
    c = app.test_client()
    c.post('/login', data={'username': user, 'password': 'admin123' if user == 'admin' else 'Operator-pass-1'})
    return c


def token(app, user='admin'):
    c = app.test_client()
    r = c.post('/api/v1/auth/login', json={'username': user,
                                           'password': 'admin123' if user == 'admin' else 'Operator-pass-1'})
    assert r.status_code == 200, r.get_json()
    return c, {'Authorization': 'Bearer ' + r.get_json()['token']}


def observe(name, hwv, at=None):
    n = node(name)
    hw.observe(n, hwv, now=at)
    db.session.commit()
    return n


# ------------------------------------------------------------------ identity

def test_the_identity_read_is_parsed_and_cleaned():
    h = hw_identity.parse(PI_OUTPUT)
    assert h['board'] == '10000000a1b2c3d4' and h['network'] == 'dc:a6:32:01:02:03'
    assert h['storage_model'] == 'SD64G 64 GB' and h['os_name'] == 'Debian GNU/Linux 12 (bookworm)'
    assert h['throttled'] == 0 and not hw_identity.undervoltage(h)
    assert hw_identity.undervoltage(hw_identity.parse('throttled=0x50005'))
    hostile = hw_identity.parse('board=<script>alert(1)</script>\nnetwork=00:00:00:00:00:00\nos=' + 'x' * 500)
    assert '<' not in hostile['board'] and hostile['network'] == '' and len(hostile['os']) <= 64
    assert hw_identity.parse('board=0000000000000000')['board'] == ''            # an all-zero serial is none


def test_the_collector_reads_hardware_with_the_metrics():
    from app.services.collector import METRICS_CMD, parse_metrics
    assert METRICS_CMD.rstrip().endswith('echo @@') and 'machine-id' in METRICS_CMD
    base = ('cpu 1 1 1 1 1 1 1\nsleep\ncpu 2 2 2 2 2 2 2\n@@\n0.1 0.2 0.3 1/2 3\n@@\nMemTotal: 1000 kB\n'
            'MemAvailable: 500 kB\n@@\n/dev/root 100 50 50 50% /\n@@\n100.0 50.0\n@@\ntemp=40.0\'C\n@@\n'
            '1 2\n@@\naarch64\n@@\n@@\n')
    m = parse_metrics(base + PI_OUTPUT + '@@\n')
    assert m['hardware']['board'] == '10000000a1b2c3d4' and m['arch'] == 'aarch64'
    assert parse_metrics(base)['hardware'] == hw_identity.parse('')                 # an older node: no section


@pytest.mark.parametrize('kinds, known, words', [
    ({'storage', 'os'}, {'board', 'storage', 'network', 'os'}, 'SD card or disk replaced'),
    ({'board', 'network'}, {'board', 'storage', 'network', 'os'}, 'Board replaced'),
    ({'network'}, {'storage', 'network', 'os'}, 'Board replaced'),                  # no board serial: MAC stands in
    ({'network'}, {'board', 'storage', 'network'}, 'Network adapter changed'),
    ({'os'}, {'board', 'os'}, 'Operating system reinstalled'),
    ({'board', 'storage'}, {'board', 'storage'}, 'Board and SD card'),
])
def test_changes_get_a_plain_name(kinds, known, words):
    assert words in hw.classify(kinds, known)


# ------------------------------------------------------------------ detection

def test_first_read_records_the_inventory_then_nothing_changes(app):
    with app.app_context():
        observe('pump-house-2', HW())
        comps = HardwareComponent.query.filter_by(node_id=node('pump-house-2').id).all()
        assert {c.kind for c in comps} == {'board', 'storage', 'network', 'os'}
        assert all(not c.installed_known for c in comps)                  # in service before EdgeShell saw it
        assert HardwareEvent.query.filter_by(kind='inventory_recorded').count() == 1
        observe('pump-house-2', HW())
        observe('pump-house-2', dict(HW(), board=''))                      # unreadable is not a change
        assert HardwareChange.query.count() == 0 and HardwareComponent.query.count() == 4


def test_an_sd_card_swap_waits_for_confirmation_then_becomes_the_record(app):
    with app.app_context():
        observe('pump-house-2', HW())
        n = observe('pump-house-2', HW(storage='card-2', os_id='machine-2'))
        ch = HardwareChange.query.filter_by(status='pending').one()
        assert 'SD card or disk replaced' in ch.summary and {i['kind'] for i in ch.items()} == {'storage', 'os'}
        assert NodeEvent.query.filter_by(kind='hardware_changed', new_value='pending').count() == 1
        assert hw.card_info([n.id])[n.id]['pending']['id'] == ch.id
        hw.decide_change(ch, 'confirm', 'op', reason='fault', note='card worn out')
        live = {c.kind: c.identifier for c in HardwareComponent.query.filter_by(node_id=n.id, removed_at=None)}
        assert live['storage'] == 'card-2' and live['os'] == 'machine-2' and live['board'] == 'board-1'
        old = HardwareComponent.query.filter_by(identifier='card-1').one()
        assert old.removed_reason == 'fault' and old.removed_at == ch.detected_at
        f = HardwareFault.query.filter_by(node_id=n.id).one()               # the failure is counted
        assert f.component == 'storage' and f.status == 'resolved' and f.reported_via == 'auto'
        with pytest.raises(hw.HardwareError):
            hw.decide_change(ch, 'confirm', 'op', reason='fault')


def test_the_old_card_in_a_new_board_is_named_and_a_moved_part_is_closed_on_its_old_node(app):
    with app.app_context():
        observe('pump-house-2', HW())
        observe('pump-house-3', HW(board='board-9', storage='card-9', network='aa:aa:aa:00:00:09', os_id='m-9'))
        observe('pump-house-2', HW(board='board-9', network='aa:aa:aa:00:00:09'))  # card into the other board
        ch = HardwareChange.query.filter_by(status='pending').one()
        assert ch.summary.startswith('Board replaced')
        assert {i['seen_on'] for i in ch.items()} == {'pump-house-3'}
        hw.decide_change(ch, 'confirm', 'op', reason='maintenance')
        moved = HardwareComponent.query.filter_by(node_id=node('pump-house-3').id, kind='board').one()
        assert moved.removed_reason == 'moved'


def test_rejecting_flags_the_node_until_the_hardware_matches_again(app):
    with app.app_context():
        observe('pump-house-2', HW())
        observe('pump-house-2', HW(board='clone', network='ee:ee:ee:00:00:01'))
        ch = HardwareChange.query.one()
        hw.decide_change(ch, 'reject', 'op', note='nobody was on site')
        n = node('pump-house-2')
        assert n.hw_alert and ch.status == 'rejected'
        assert HardwareComponent.query.filter_by(node_id=n.id, kind='board', removed_at=None).one().identifier == 'board-1'
        observe('pump-house-2', HW())                                  # the real hardware is back
        assert not node('pump-house-2').hw_alert


def test_a_change_that_goes_away_is_closed_as_reverted(app):
    with app.app_context():
        observe('pump-house-2', HW())
        observe('pump-house-2', HW(storage='temp-card', os_id='m-x'))
        observe('pump-house-2', HW())
        assert HardwareChange.query.one().status == 'reverted'


def test_low_power_is_logged_once_a_day_and_hints_power_faults(app):
    with app.app_context():
        t = datetime.utcnow()
        observe('pump-house-2', HW(throttled=0x50005), at=t)
        observe('pump-house-2', HW(throttled=0x50000), at=t + timedelta(hours=2))
        assert HardwareEvent.query.filter_by(kind='undervoltage').count() == 1
        observe('pump-house-2', HW(throttled=0x50000), at=t + timedelta(hours=25))
        assert HardwareEvent.query.filter_by(kind='undervoltage').count() == 2
        f = hw.report_fault([node('pump-house-2')], 'power', 'Reboots at random', 'mon')[0]
        assert f.undervoltage_hint
        assert not hw.report_fault([node('gate-1')], 'power', 'Dead', 'mon')[0].undervoltage_hint


# ------------------------------------------------------------------ faults and roles

def test_monitors_report_and_observe_operators_resolve(app):
    mon, op = browser(app, 'mon'), browser(app, 'op')
    with app.app_context():
        ids = [node('pump-house-2').id, node('gate-1').id]
    r = mon.post('/hardware/api/report', json={'node_ids': ids, 'component': 'power', 'symptom': 'No lights',
                                               'severity': 'high'})
    assert r.status_code == 200 and len(r.get_json()['faults']) == 2
    fid = r.get_json()['faults'][0]
    assert mon.get(f'/hardware/api/faults/{fid}').status_code == 200                      # observe
    assert mon.post(f'/hardware/api/faults/{fid}', json={'action': 'resolve', 'resolution': 'replaced'}).status_code == 403
    assert mon.post(f'/hardware/api/faults/{fid}', json={'action': 'note', 'note': 'x'}).status_code == 403
    assert op.post(f'/hardware/api/faults/{fid}', json={'action': 'progress', 'note': 'engineer on the way'}).get_json()['success']
    r = op.post(f'/hardware/api/faults/{fid}', json={'action': 'resolve', 'resolution': 'replaced',
                                                     'new_serial': 'PSU-77', 'note': 'new 3 A supply'})
    d = r.get_json()['fault']
    assert d['status'] == 'resolved' and d['resolved_by'] == 'op' and d['hours_to_fix'] is not None
    assert [e['kind'] for e in d['timeline']] == ['fault_reported', 'fault_in_progress', 'fault_resolved']
    with app.app_context():
        psu = HardwareComponent.query.filter_by(kind='power', removed_at=None).one()  # its service life starts
        assert psu.identifier == 'PSU-77' and psu.source == 'manual'
    assert op.post(f'/hardware/api/faults/{fid}', json={'action': 'reopen'}).get_json()['fault']['status'] == 'open'
    assert mon.post('/hardware/api/report', json={'node_ids': ids, 'component': 'nope', 'symptom': 'x'}).status_code == 400
    assert mon.post('/hardware/api/report', json={'node_ids': [99999], 'component': 'power', 'symptom': 'x'}).status_code == 400
    assert app.test_client().post('/hardware/api/report', json={'node_ids': ids}).status_code in (302, 401)


def test_downtime_counts_minutes_down_without_a_heartbeat(app):
    with app.app_context():
        n = node('gate-1')
        start = datetime.utcnow() - timedelta(minutes=30)
        for i in range(30):
            db.session.add(SystemMetrics(node_id=n.id, timestamp=start + timedelta(minutes=i),
                                         status='offline' if i < 20 else 'online', heartbeat=i < 5))
        db.session.commit()
        assert hw.downtime_minutes(n.id, start, datetime.utcnow()) == 15


def test_only_operators_decide_changes_and_link_replacements(app):
    with app.app_context():
        observe('pump-house-2', HW())
        observe('pump-house-2', HW(storage='c2', os_id='m2'))
        cid = HardwareChange.query.one().id
        a, b = node('pump-house-2').id, node('gate-1').id
    mon, op = browser(app, 'mon'), browser(app, 'op')
    assert mon.post(f'/hardware/api/changes/{cid}', json={'decision': 'confirm', 'reason': 'fault'}).status_code == 403
    assert mon.post(f'/hardware/api/node/{a}/replaces', json={'replaces_node_id': b}).status_code == 403
    assert op.post(f'/hardware/api/changes/{cid}', json={'decision': 'confirm'}).status_code == 400    # needs a reason
    assert op.post(f'/hardware/api/changes/{cid}', json={'decision': 'confirm', 'reason': 'upgrade'}).get_json()['success']


# ------------------------------------------------------------------ replacements

def test_a_new_node_is_offered_the_node_it_probably_replaces(app):
    with app.app_context():
        observe('gate-1', HW(board='g-old', storage='card-g', network='aa:00:00:00:00:01', os_id='mg'))
        new = Node(name='gate-1-new', ip_address='10.0.0.9', username='pi', status='online', location='South site',
                   node_type='edge')
        new.password = 'x'
        db.session.add(new)
        db.session.commit()
        observe('gate-1-new', HW(board='g-new', storage='card-g', network='bb:00:00:00:00:01', os_id='mg'))
        new = node('gate-1-new')
        best = hw.candidates(new)[0]
        assert best['name'] == 'gate-1' and any(r.startswith('same sd card or disk') for r in best['reasons'])
        assert 'same location' in best['reasons'] and new.hw_replace_prompt == 'pending'


def test_linking_a_replacement_retires_the_old_node_and_keeps_one_history(app):
    with app.app_context():
        observe('gate-1', HW(board='g-old', storage='card-g', network='aa:00:00:00:00:01', os_id='mg'))
        new = Node(name='gate-1b', ip_address='10.0.0.9', username='pi', status='online', location='South site')
        new.password = 'x'
        db.session.add(new)
        db.session.commit()
        observe('gate-1b', HW(board='g-new', storage='card-new', network='bb:00:00:00:00:01', os_id='m-new'))
        old_id, new_id = node('gate-1').id, node('gate-1b').id
    op = browser(app, 'op')
    r = op.post(f'/hardware/api/node/{new_id}/replaces', json={'replaces_node_id': old_id, 'failed': ['board', 'power'],
                                                               'note': 'water damage'})
    assert r.get_json()['success'], r.get_json()
    with app.app_context():
        old, new = db.session.get(Node, old_id), db.session.get(Node, new_id)
        assert old.retired_at and not old.is_active and old.status == 'retired' and old.replaced_by_id == new_id
        assert new.replaces_id == old_id and new.hw_replace_prompt == 'done'
        reasons = {c.kind: c.removed_reason for c in HardwareComponent.query.filter_by(node_id=old_id)}
        assert reasons == {'board': 'fault', 'storage': 'retired', 'network': 'retired', 'os': 'retired'}
        assert {f.component for f in HardwareFault.query.filter_by(node_id=old_id)} == {'board', 'power'}
        hist = hw.node_history(new)
        assert [x['name'] for x in hist['lineage']] == ['gate-1b', 'gate-1']
        assert {c['node_name'] for c in hist['components']} == {'gate-1', 'gate-1b'}
        with pytest.raises(hw.HardwareError):
            hw.link_replacement(new, old, 'op')                                   # already replaced
        with pytest.raises(hw.HardwareError):
            hw.link_replacement(old, new, 'op')                                   # loop
        from app.services.collector import COLLECTOR
        assert old_id not in COLLECTOR.run_cycle(force=True)                    # no longer checked


# ------------------------------------------------------------------ numbers

def test_the_fleet_numbers(app):
    with app.app_context():
        now = datetime.utcnow()
        observe('pump-house-2', HW(), at=now - timedelta(days=100))
        n = node('pump-house-2')
        for c in HardwareComponent.query.filter_by(node_id=n.id):
            c.installed_known, c.installed_at = True, now - timedelta(days=100)
        observe('pump-house-2', HW(storage='card-2', os_id='m2'), at=now - timedelta(days=10))
        hw.decide_change(HardwareChange.query.one(), 'confirm', 'op', reason='fault')
        f = hw.report_fault([node('gate-1')], 'power', 'Dead', 'mon')[0]
        f.created_at = now - timedelta(hours=6)
        db.session.commit()
        hw.update_fault(f, 'resolve', 'op', resolution='replaced')
        s = hw.stats(90)
        assert s['faults'] == 2 and s['nodes'] == 3 and s['faults_per_100_nodes_30d'] == pytest.approx(66.7)
        assert s['mean_hours_to_fix'] == pytest.approx(6, abs=0.1)              # the auto record is left out
        life = {x['kind']: x for x in s['service_life']}
        assert life['storage']['failed'] == 1 and life['storage']['mean_days_to_failure'] == pytest.approx(90, abs=0.1)
        assert s['replacements'] == 1 and s['open'] == 0
        assert {r['node_name'] for r in s['top_nodes']} == {'pump-house-2', 'gate-1'}
        assert s['monthly'][-1]['total'] >= 1 and len(s['monthly']) == 12


def test_csv_exports_never_run_as_spreadsheet_formulas(app):
    with app.app_context():
        hw.report_fault([node('gate-1')], 'other', '=HYPERLINK("http://evil")', 'mon')
        text = hw.export_csv('faults')
        assert "'=HYPERLINK" in text and ',=HYPERLINK' not in text
    r = browser(app, 'mon').get('/hardware/export.csv?what=components')
    assert r.status_code == 200 and r.mimetype == 'text/csv'


# ------------------------------------------------------------------ API

def test_api_report_permission_and_resolution(app):
    c, mon = token(app, 'mon')
    with app.app_context():
        nid = node('gate-1').id
    r = c.post('/api/v1/hardware/faults', headers=mon, json={'node_name': 'gate-1', 'component': 'storage',
                                                              'symptom': 'Read-only file system'})
    assert r.status_code == 201, r.get_json()
    fid = r.get_json()['id']
    assert r.get_json()['reported_via'] == 'api'
    assert c.patch(f'/api/v1/hardware/faults/{fid}', headers=mon, json={'action': 'resolve',
                                                                      'resolution': 'replaced'}).status_code == 403
    c2, op = token(app, 'op')
    r = c2.patch(f'/api/v1/hardware/faults/{fid}', headers=op, json={'action': 'resolve', 'resolution': 'replaced'})
    assert r.status_code == 200 and r.get_json()['status'] == 'resolved'
    assert c.get(f'/api/v1/nodes/{nid}/hardware', headers=mon).get_json()['faults'][0]['id'] == fid
    assert c.get('/api/v1/hardware/stats', headers=mon).status_code == 200
    ev = c.get('/api/v1/events?kind=hardware_fault', headers=mon).get_json()['events']
    assert ev and ev[0]['node_name'] == 'gate-1'
    assert c.post('/api/v1/hardware/faults', headers=mon, json={'component': 'power', 'symptom': 'x'}).status_code == 400
    assert c.post('/api/v1/hardware/faults', headers=mon, json={'node_name': 'gate-1', 'component': 'power',
                                                                'symptom': 'x', 'extra': 1}).status_code == 400
    doc = c.get('/api/v1/openapi.json').get_json()
    assert '/hardware/faults' in doc['paths'] and '/nodes/{node_id}/replaces' in doc['paths']


def test_a_key_without_report_cannot_report(app):
    from app.services import api_auth
    with app.test_request_context():
        u = User.query.filter_by(username='mon').first()
        _, secret = api_auth.issue(u, 'key', scopes=('read',), name='dash',
                                   expires_at=datetime.utcnow() + timedelta(days=1))
        db.session.commit()
    c = app.test_client()
    r = c.post('/api/v1/hardware/faults', headers={'Authorization': 'Bearer ' + secret},
               json={'node_name': 'gate-1', 'component': 'power', 'symptom': 'x'})
    assert r.status_code == 403


def test_older_saved_api_policies_get_the_report_permission(app):
    from app.models import Settings
    from app.services import api_auth
    with app.app_context():
        db.session.add(Settings(key=api_auth.POLICY_KEY, value=json.dumps({'enabled': True, 'scopes': ['read'],
                                                                           'keys': True})))
        db.session.commit()
        assert api_auth.policy()['scopes'] == ['read', 'report']
        api_auth.save_policy(True, ['read'], True)                     # turned off on purpose stays off
        assert api_auth.policy()['scopes'] == ['read']


# ------------------------------------------------------------------ webhooks

def fake_dns(ip):
    def resolve(host, port, **kw):
        literal = host.replace('.', '').isdigit()               # an IP address resolves to itself
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (host if literal else ip, port))]
    return resolve


@pytest.mark.parametrize('url, ip, allowed', [
    ('https://hooks.example.com/x', '93.184.216.34', True),
    ('http://hooks.example.com/x', '93.184.216.34', False),             # plain http to the internet
    ('https://hooks.example.com/x', '169.254.169.254', False),          # cloud metadata
    ('https://hooks.example.com/x', '127.0.0.1', False),
    ('https://hooks.example.com/x', '10.1.2.3', False),
    ('https://hooks.example.com/x', '100.101.102.103', False),          # the mesh (CGNAT range)
    ('https://user:pw@hooks.example.com/x', '93.184.216.34', False),
    ('ftp://hooks.example.com/x', '93.184.216.34', False),
])
def test_webhook_addresses_inside_the_server_are_refused(app, monkeypatch, url, ip, allowed):
    monkeypatch.setattr(webhook_service.socket, 'getaddrinfo', fake_dns(ip))
    with app.app_context():
        if allowed:
            assert webhook_service.check_url(url)
        else:
            with pytest.raises(ValueError):
                webhook_service.check_url(url)


def test_private_receivers_only_when_allowed(app, monkeypatch):
    monkeypatch.setattr(webhook_service.socket, 'getaddrinfo', fake_dns('192.168.1.20'))
    with app.app_context():
        assert webhook_service.check_url('http://tickets.lan/hook', allow_private=True)
        with pytest.raises(ValueError):
            webhook_service.check_url('http://169.254.169.254/', allow_private=True)


def test_webhooks_are_signed_and_the_secret_is_shown_once(app, monkeypatch):
    monkeypatch.setattr(webhook_service.socket, 'getaddrinfo', fake_dns('93.184.216.34'))
    sent = []

    class Resp:
        status_code = 204

    import requests
    monkeypatch.setattr(requests, 'post', lambda url, data, **kw: sent.append((url, data, kw['headers'])) or Resp())
    admin, op = browser(app), browser(app, 'op')
    assert op.post('/hardware/api/webhooks', json={'name': 'x', 'url': 'https://hooks.example.com/x',
                                                   'events': ['hardware.fault_reported']}).status_code == 403
    r = admin.post('/hardware/api/webhooks', json={'name': 'tickets', 'url': 'https://hooks.example.com/x',
                                                   'events': ['hardware.fault_reported', 'bogus']})
    secret = r.get_json()['secret']
    assert secret.startswith('whsec_') and r.get_json()['webhook']['events'] == ['hardware.fault_reported']
    listing = admin.get('/hardware/api/webhooks').get_data(as_text=True)
    assert secret not in listing
    with app.app_context():
        assert secret not in (Webhook.query.one()._secret or '')                 # encrypted at rest
        hw.report_fault([node('gate-1')], 'power', 'Dead', 'mon')
    url, body, headers = sent[-1]
    expected = 'sha256=' + hmac.new(secret.encode(), headers['X-EdgeShell-Timestamp'].encode() + b'.' + body,
                                    hashlib.sha256).hexdigest()
    assert headers['X-EdgeShell-Signature'] == expected and headers['X-EdgeShell-Event'] == 'hardware.fault_reported'
    payload = json.loads(body)
    assert payload['data']['node_name'] == 'gate-1' and payload['data']['component_label'] == 'Power supply'
    with app.app_context():
        assert WebhookDelivery.query.one().status_code == 204
        hid = Webhook.query.one().id
    assert admin.post(f'/hardware/api/webhooks/{hid}', json={'action': 'test'}).get_json()['success']


# ------------------------------------------------------------------ pages

def test_cards_show_hardware_state_and_the_page_opens_for_everyone(app):
    with app.app_context():
        observe('pump-house-2', HW())
        observe('pump-house-2', HW(storage='c2', os_id='m2'))
        hw.report_fault([node('gate-1')], 'power', 'Dead', 'mon')
    mon = browser(app, 'mon')                         # one session per account: reuse it
    page = mon.get('/nodes/').get_data(as_text=True)
    assert 'Hardware changed' in page and '1 open fault' in page and 'es-hw-report' in page
    for c in (mon, browser(app, 'op'), browser(app)):
        r = c.get('/hardware/')
        assert r.status_code == 200 and 'hwReportBtn' in r.get_data(as_text=True)
    assert mon.get('/hardware/api/stats').get_json()['open'] == 1


def test_errors_are_fixed_texts_never_an_exception_s_own_text(app):
    import re
    from pathlib import Path
    from app.services import hw_errors
    root = Path(__file__).resolve().parents[1] / 'app'
    codes = set()
    for f in ('services/hardware_service.py', 'services/webhook_service.py'):
        src = (root / f).read_text(encoding='utf-8')
        codes |= set(re.findall(r"raise HardwareError\('([a-z_]+)'\)", src))
        assert not re.search(r'raise (HardwareError|ValueError)\(f?["\'][A-Z]', src), f   # texts live in hw_errors
    assert codes and codes <= set(hw_errors.MESSAGES), codes - set(hw_errors.MESSAGES)
    for f in ('routes/hardware.py',):
        assert 'str(e)' not in (root / f).read_text(encoding='utf-8')
    op = browser(app, 'op')
    with app.app_context():
        nid = node('gate-1').id
    r = op.post('/hardware/api/report', json={'node_ids': [nid], 'component': 'power', 'symptom': ''})
    assert r.status_code == 400 and r.get_json()['message'] == hw_errors.MESSAGES['symptom']
    assert hw_errors.public_message(ValueError('/etc/secret path')) == hw_errors.DEFAULT
